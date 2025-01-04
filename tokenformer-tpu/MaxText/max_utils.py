"""
Copyright 2023 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

""" Common Max Utils needed by multiple modules"""
import checkpointing
import common_types
import functools
import time
import socket
import subprocess
from etils import epath

import max_logging

import numpy as np
import jax
import jax.numpy as jnp
from jax.experimental import mesh_utils
import orbax.checkpoint as ocp
import orbax

from jax.experimental.array_serialization import serialization as array_serial
import tensorflow.io.gfile as gfile
import dataclasses
import flax
from typing import Mapping
import re
import collections

import json
import yaml
import flax
from flax.training import train_state
from flax import linen as nn
from flax.linen import partitioning as nn_partitioning

import optax
import os
from typing import Tuple
from tensorboardX import writer

from google.cloud import storage


def find_nans_and_infs(pytree):
  def finder(x):
    return jnp.any(jnp.isinf(x) | jnp.isnan(x))

  bad_pytree = jax.tree_util.tree_map(finder, pytree)
  return jax.tree_util.tree_flatten(bad_pytree)


def l2norm_pytree(x):
  """L2 norm of a pytree of arrays."""
  return jnp.sqrt(
      jax.tree_util.tree_reduce(
          lambda x, y: x + jnp.sum(jnp.square(y)), x, initializer=0.0
      )
  )


def calculate_num_params_from_pytree(params):
  params_sizes = jax.tree_util.tree_map(jax.numpy.size, params)
  total_parameters = jax.tree_util.tree_reduce(lambda x, y: x + y, params_sizes)
  assert total_parameters >= 0
  return total_parameters


def calculate_total_params_per_chip(params):
  """Calculate total paramsper chip."""
  def calculate_leaf_params_per_chip(arr):
    shard = arr.addressable_shards[0]
    return np.prod(shard.data.shape)

  params_sizes_per_chip = jax.tree_util.tree_map(
      calculate_leaf_params_per_chip, params
  )
  total_parameters_per_chip = jax.tree_util.tree_reduce(
      lambda x, y: x + y, params_sizes_per_chip
  )
  return total_parameters_per_chip


def calculate_bytes_from_pytree(params):
  params_bytes = jax.tree_util.tree_map(lambda x: x.nbytes, params)
  total_bytes = jax.tree_util.tree_reduce(lambda x, y: x + y, params_bytes)
  return total_bytes


def summarize_size_from_pytree(params):
  num_params = calculate_num_params_from_pytree(params)
  num_bytes = calculate_bytes_from_pytree(params)
  return num_params, num_bytes, num_bytes / num_params


def initialize_summary_writer(config):
  return (
      writer.SummaryWriter(config.tensorboard_dir)
      if jax.process_index() == 0
      else None
  )


def close_summary_writer(summary_writer):
  if jax.process_index() == 0:
    summary_writer.close()


def _prepare_metrics_for_json(metrics, step, run_name):
  """Converts metric dictionary into json supported types (e.g. float)"""
  metrics_dict = {}
  for val in metrics["scalar"]:
    metrics_dict[val] = float(metrics["scalar"][val])
  metrics_dict["step"] = float(step)
  metrics_dict["run_name"] = run_name
  return metrics_dict


def write_metrics_locally(metrics, step, config, file):
  """Writes metrics locally for testing"""
  if step == 0:
    file.truncate(0)

  metrics_dict = _prepare_metrics_for_json(metrics, step, config.run_name)
  file.write(str(json.dumps(metrics_dict)) + "\n")

  if step == config.steps - 1:
    file.close()


def add_config_to_summary_writer(config, summary_writer):
  """Writes config params to tensorboard"""
  if jax.process_index() == 0:
    for key, value in config.get_keys().items():
      add_text_to_summary_writer(key, str(value), summary_writer)


def add_text_to_summary_writer(key, value, summary_writer):
  """Writes given key-value pair to tensorboard as text/summary"""
  if jax.process_index() == 0:
    summary_writer.add_text(key, value)


def write_metrics_for_gcs(metrics, step, config, running_metrics):
  """Writes metrics to gcs"""
  metrics_dict_step = _prepare_metrics_for_json(metrics, step, config.run_name)
  running_metrics.append(metrics_dict_step)
  if (step + 1) % config.log_period == 0 or step == config.steps - 1:
    start_step = (step // config.log_period) * config.log_period
    metrics_filename = f"metrics_step_{start_step:06}_to_step_{step:06}.txt"
    with open(metrics_filename, "w", encoding="utf8") as metrics_for_gcs:
      for metrics_step in running_metrics:
        metrics_for_gcs.write(str(json.dumps(metrics_step)) + "\n")

    metrics_for_gcs.close()
    gcs_filename = os.path.join(config.metrics_dir, metrics_filename)
    max_logging.log(f"Moving file {metrics_filename} to GCS...")
    upload_blob(gcs_filename, metrics_filename)
    max_logging.log(f"File {metrics_filename} moved successfully!")
    running_metrics = []  # reset running_metrics to empty list
  return running_metrics


def write_config_raw_keys_for_gcs(raw_keys):
  """Writes config raw keys to GCS"""
  if not raw_keys["save_config_to_gcs"] or jax.process_index() != 0:
    return
  max_logging.log("Writing config to GCS...")

  raw_keys_dict = dict(raw_keys)
  filename = "config.yml"
  with open(filename, "w", encoding="utf8") as config_for_gcs:
    yaml.dump(raw_keys_dict, config_for_gcs)
  config_for_gcs.close()

  gcs_filename = os.path.join(
      raw_keys["base_output_directory"], raw_keys["run_name"], filename
  )
  max_logging.log(f"Moving file {filename} to GCS...")
  upload_blob(gcs_filename, filename)
  max_logging.log(f"File {filename} moved successfully!")


def parse_gcs_bucket_and_prefix(destination_gcs_name):
  path_parts = destination_gcs_name.replace("gs://", "").split("/")
  bucket = path_parts.pop(0)
  key = "/".join(path_parts)
  return bucket, key


def upload_blob(destination_gcs_name, source_file_name):
  """Uploads a file to a GCS location"""
  bucket_name, prefix_name = parse_gcs_bucket_and_prefix(destination_gcs_name)
  storage_client = storage.Client()
  bucket = storage_client.get_bucket(bucket_name)
  blob = bucket.blob(prefix_name)
  blob.upload_from_filename(source_file_name)


def maybe_initialize_jax_distributed_system(raw_keys):
  """The best recipe to initialize the Jax Distributed System has varied over time. We keep a layer of
  indirection in MaxText to avoid breaking the call sites unnecessarily.

  Currently jax.distributed.initialize() fully works as expected!

  For CPUs, we call jax.distributed.initialize() explicitly, with the specified arguments.
  """
  if is_gpu_backend(raw_keys):
    max_logging.log(
        "Attempting to initialize the jax distributed system for GPU backend..."
    )
    initialize_jax_for_gpu()
    max_logging.log("Jax distributed system initialized on GPU!")
  elif is_cpu_backend(raw_keys):
    max_logging.log(
        "Attempting to initialize the jax distributed system for CPU backend..."
    )
    initialize_jax_for_cpu()
    max_logging.log("Jax distributed system initialized on CPUs!")
  elif (
      raw_keys["enable_checkpointing"]
      and raw_keys["async_checkpointing"]
      and raw_keys["compile_topology_num_slices"] == -1
      and not raw_keys["enable_single_controller"]
  ) or raw_keys["hardware"] == "gpu_multiprocess":
    max_logging.log("Attempting to initialize the jax distributed system...")
    jax.distributed.initialize()
    ocp.multihost.utils.initialize_runtime_to_distributed_ids()
    max_logging.log("Jax distributed system initialized!")


def initialize_jax_for_gpu():
  """Jax distributed initialize for GPUs."""
  if os.environ.get("JAX_COORDINATOR_IP") is not None:
    coordinator_ip = str(os.getenv("JAX_COORDINATOR_IP"))
    coordinator_port = str(os.getenv("JAX_COORDINATOR_PORT"))
    jax.distributed.initialize(
        coordinator_address=f"{coordinator_ip}:{coordinator_port}",
        num_processes=int(os.getenv("NNODES")),
        process_id=int(os.getenv("NODE_RANK")),
    )
    max_logging.log(f"JAX global devices: {jax.devices()}")


def initialize_jax_for_cpu():
  """Jax distributed initialize for CPUs. Includes retries until the coordinator is ready."""
  coordinator_ip_address = get_coordinator_ip_address()
  coordinator_address = (
      coordinator_ip_address + ":1234"
  )  # JAX coordinator port used in XPK
  # Env variables to be set in XPK or otherwise
  job_index = int(os.environ.get("JOB_INDEX"))
  job_completion_index = int(os.environ.get("JOB_COMPLETION_INDEX"))
  processes_in_job = int(os.environ.get("PROCESSES_IN_JOB"))
  pid = job_index * processes_in_job + job_completion_index
  max_logging.log(f" Jax process id is {pid} ")
  # Explicit initialize is needed only for CPUs
  jax.distributed.initialize(
      coordinator_address=coordinator_address,
      process_id=pid,
      num_processes=int(os.environ.get("JAX_PROCESS_COUNT")),
  )


def is_cpu_backend(raw_keys):
  """Determine whether Maxtext is intended to run on a CPU backend."""
  return raw_keys["hardware"] == "cpu"


def is_gpu_backend(raw_keys):
  """Determine whether Maxtext is intended to run on a GPU backend."""
  return raw_keys["hardware"] == "gpu"


def get_coordinator_ip_address():
  """Get coordinator IP Address with retries"""
  coordinator_address = ""
  coordinator_ip_address = ""
  if os.environ.get("JAX_COORDINATOR_ADDRESS") is not None:
    coordinator_address = os.environ.get("JAX_COORDINATOR_ADDRESS")
    coordinator_found = False
    lookup_attempt = 1
    max_coordinator_lookups = 50
    while not coordinator_found and lookup_attempt <= max_coordinator_lookups:
      try:
        coordinator_ip_address = socket.gethostbyname(coordinator_address)
        coordinator_found = True
      except socket.gaierror:
        max_logging.log(
            f"Failed to recognize coordinator address {coordinator_address} on attempt {lookup_attempt}, retrying..."
        )
        lookup_attempt += 1
        time.sleep(5)
  max_logging.log(f"Coordinator IP address: {coordinator_ip_address}")
  return coordinator_ip_address


def fill_unspecified_mesh_axes(
    parallelism_vals, target_product, parallelism_type
):
  """Evaluates unspecified DCN/ICI parallelism values"""
  if -1 in parallelism_vals:
    assert (
        parallelism_vals.count(-1) == 1
    ), f"Found unspecified values (-1) for more than one {parallelism_type}\
      parallelism axis. At most one axis can be unspecified."

    determined_val = target_product / np.prod(parallelism_vals) * -1

    assert (
        determined_val >= 1 and determined_val.is_integer
    ), f"Unspecified value unable to be determined with the given\
      {parallelism_type} parallelism values"

    parallelism_vals[parallelism_vals.index(-1)] = int(determined_val)

  target_type = "slices" if parallelism_type == "DCN" else "devices per slice"
  assert (
      np.prod(parallelism_vals) == target_product
  ), f"Number of {target_type} {target_product} does not match\
    the product of the {parallelism_type} parallelism {np.prod(parallelism_vals)}"

  return parallelism_vals


def create_device_mesh(config, devices=None):
  """Creates a device mesh with each slice in its own data parallel group. If there is only one slice, uses two replicas"""
  if devices is None:
    devices = jax.devices()
  num_devices = len(devices)
  num_slices = config.num_slices
  num_devices_per_slice = num_devices // num_slices

  multi_slice_env = num_slices > 1

  dcn_parallelism = [
      config.dcn_data_parallelism,
      config.dcn_pipeline_parallelism,
      config.dcn_fsdp_parallelism,
      config.dcn_fsdp_transpose_parallelism,
      config.dcn_sequence_parallelism,
      config.dcn_tensor_parallelism,
      config.dcn_autoregressive_parallelism,
  ]
  ici_parallelism = [
      config.ici_data_parallelism,
      config.ici_pipeline_parallelism,
      config.ici_fsdp_parallelism,
      config.ici_fsdp_transpose_parallelism,
      config.ici_sequence_parallelism,
      config.ici_tensor_parallelism,
      config.ici_autoregressive_parallelism,
  ]

  # Find possible unspecified parallelisms
  ici_parallelism = fill_unspecified_mesh_axes(
      ici_parallelism, num_devices_per_slice, "ICI"
  )

  allow_split_physical_axes = (
      config.allow_split_physical_axes
      if config.allow_split_physical_axes
      else False
  )

  if multi_slice_env:
    dcn_parallelism = fill_unspecified_mesh_axes(
        dcn_parallelism, num_slices, "DCN"
    )
    mesh = mesh_utils.create_hybrid_device_mesh(
        ici_parallelism,
        dcn_parallelism,
        devices,
    )
  else:
    if allow_split_physical_axes:
      mesh = mesh_utils.create_device_mesh(
          ici_parallelism,
          devices,
          contiguous_submeshes=False,
          allow_split_physical_axes=allow_split_physical_axes,
      )
    else:
      mesh = mesh_utils.create_device_mesh(
            ici_parallelism,
            devices,
        )

  max_logging.log(f"Num_devices: {num_devices}, shape {mesh.shape}")

  return mesh


def unbox_logicallypartioned(boxed_pytree):
  """Unboxes the flax.LogicallyPartitioned pieces

  Args:
    boxed_pytree: a pytree that includes LogicallyPartitioned
      leaves.
  Returns:
    a pytree where all all LogicallyPartitioned leaves have been unboxed.
  """
  return jax.tree_util.tree_map(
      lambda x: x.unbox()
      if isinstance(x, flax.linen.spmd.LogicallyPartitioned)
      else x,
      boxed_pytree,
      is_leaf=lambda k: isinstance(k, flax.linen.spmd.LogicallyPartitioned),
  )


def init_decode_state(apply_fn, params):
  """Init train state with null opt state for decode."""
  state = train_state.TrainState(step=0, apply_fn=apply_fn, params=params, tx=None, opt_state={})  # type: ignore
  return state


def init_training_state(apply_fn, params, tx):
  """Init train state with null opt state for decode."""
  state = train_state.TrainState.create(apply_fn=apply_fn, params=params, tx=tx)
  return state


def init_initial_state(model, tx, config, is_training, key):
  """
  We pass in "static" objects like model, tx, config as JAX compares them by
  object hash, and instantiating them inside causes pjit top-level annotations
  to fail to match as pytree prefixes if we re-instantiate.

  Args: model, tx, config, is_training, key
  """
  input_shape = (config.global_batch_size_to_load, config.max_target_length)
  model_vars = model.init(
      {"params": key, "dropout": key, "aqt": key},
      jnp.ones(input_shape, dtype=jnp.int32),
      jnp.ones(input_shape, dtype=jnp.int32),
  )
  if is_training:
    return init_training_state(model.apply, model_vars, tx)
  return init_decode_state(model.apply, model_vars)



def setup_decode_state(model, config, rng, mesh, checkpoint_manager):
  """Setup decode state by loading params from a checkpoint.
  Args:
    model: the flax model to initialize
    config: config object
    rng: jax.prng key
    mesh: jax.devices() mesh
    checkpoint_manager: Checkpoint manager

  Returns:
    state: state with decode params loaded from the checkpoint
    state_mesh_annotations: the mesh annotations for the state
  """
  if not config.load_parameters_path:
    # generate random params
    max_logging.log(
        "No decode checkpoint specified - generating random weights."
    )
    state, state_mesh_annotations, _ = setup_initial_state(
      model, None, None, config, rng, mesh, checkpoint_manager, False
      )
  else:
    # Load params from checkpoint
    max_logging.log(f"Loading decode params from {config.load_parameters_path}")
    unboxed_abstract_state, state_mesh_annotations, _ = (
      get_abstract_state(model, None, config, rng, mesh, False)
      )
    with nn_partitioning.axis_rules(config.logical_axis_rules):
      params = checkpointing.load_params_from_path(
        config.load_parameters_path,
        unboxed_abstract_state.params
        )
    state = init_decode_state(None, params)

  state = unbox_logicallypartioned(state)
  return state, state_mesh_annotations


def setup_training_state(
    model, data_iterator, tx, config, rng, mesh, checkpoint_manager
):
  is_training = True
  return setup_initial_state(
      model,
      data_iterator,
      tx,
      config,
      rng,
      mesh,
      checkpoint_manager,
      is_training,
  )


def setup_initial_state(
    model,
    data_iterator,
    tx,
    config,
    rng,
    mesh,
    checkpoint_manager,
    is_training=True,
):
  """We initialize the model and optimizer state, and optionally load from a
  checkpoint as necessary.

  Args:
    model: the flax model to initialize
    tx: the optax.GradientTransformation
    config: config object
    rng: jax.prng key
    mesh: jax.devices() mesh
    checkpoint_manager: an Orbax checkpointing.CheckpointManager object
    is_training: True to initialize training state, False for decode state

  Returns:
    state: the initialized train state
    state_mesh_annotations: the mesh annotations for the train state
  """

  unboxed_abstract_state, state_mesh_annotations, state_mesh_shardings = (
      get_abstract_state(model, tx, config, rng, mesh, is_training)
  )

  # Initialization
  with nn_partitioning.axis_rules(config.logical_axis_rules):
    restored, raw_params = checkpointing.load_state_if_possible(
        checkpoint_manager,
        data_iterator,
        config.load_parameters_path,
        config.load_full_state_path,
        unboxed_abstract_state,
        config.enable_single_replica_ckpt_restoring,
        config.dataset_type,
    )

    if restored:
      if "iter" in restored and restored["iter"] is not None:
        data_iterator.local_iterator = restored["iter"]
      state = restored["items"]
      state = unbox_logicallypartioned(state)
    else:
      init_state_partial = functools.partial(
          init_initial_state, model, tx, config, is_training
      )
      state = jax.jit(
          init_state_partial,
          in_shardings=None,
          out_shardings=state_mesh_shardings,
      )(rng)
      state = unbox_logicallypartioned(state)
      if raw_params:  # If we loaded a partial state, we need to merge it.
        if config.load_parameters_zero_init:
          reused_params = raw_params
        else:
          # when parameter reusing, partially resume the small model's params to the big model
          slot_num = config.load_parameters_path_slot_number
          assert slot_num != -1, "Please specify the load_parameters_path_slot_number manually!!!"
          slot_num_tree = jax.tree_util.tree_map(lambda x: slot_num, raw_params)
          reused_params = jax.tree_util.tree_map_with_path(helper, raw_params, state.params, slot_num_tree)
        
        state = state.replace(params=reused_params)

  return state, state_mesh_annotations, data_iterator, unboxed_abstract_state


def helper(kp, raw_params, state_params, slot_num):
  """This function takes in 3 pytree of the same struct and then reuse the parameters for tokenformer
  Note this function should be used together with jax.tree_util.tree_map_with_path
  For example:
  slot_num_tree = jax.tree_util.tree_map(lambda x: slot_num, raw_params)
  reused_params = jax.tree_util.tree_map_with_path(helper, raw_params, state.params, slot_num_tree)
  """
  kp = jax.tree_util.keystr(kp)
  print(kp, raw_params.shape, state_params.shape, slot_num)
  if "['params']['decoder']['layers']['mlp']['wi']['kernel']" in kp:
    return state_params.at[:, :, :4 * slot_num].set(raw_params[:, :, :4 * slot_num])
  elif "['params']['decoder']['layers']['mlp']['wo']['kernel']" in kp:
    return state_params.at[:4 * slot_num, :, :].set(raw_params[:4 * slot_num, :, :])
  elif "['params']['decoder']['layers']['self_attention']['qkv_key_slot']['kernel']" in kp:
    return state_params.at[:, :, :, :slot_num].set(raw_params[:, :, :, :slot_num])
  elif "['params']['decoder']['layers']['self_attention']['q_value_slot']['kernel']" in kp:
    return state_params.at[:slot_num, :, :].set(raw_params[:slot_num, :, :])
  elif "['params']['decoder']['layers']['self_attention']['k_value_slot']['kernel']" in kp:
    return state_params.at[:slot_num, :, :].set(raw_params[:slot_num, :, :])
  elif "['params']['decoder']['layers']['self_attention']['v_value_slot']['kernel']" in kp:
    return state_params.at[:slot_num, :, :].set(raw_params[:slot_num, :, :])
  elif "['params']['decoder']['layers']['self_attention']['proj_key_slot']['kernel']" in kp:
    return state_params.at[:, :, :slot_num].set(raw_params[:, :, :slot_num])
  elif "['params']['decoder']['layers']['self_attention']['proj_value_slot']['kernel']" in kp:
    return state_params.at[:slot_num, :, :].set(raw_params[:slot_num, :, :])
  else:
    return raw_params
  

def load_checkpoint_ts(path, **tsload_kw):
  """Loads a big_vision checkpoint saved by `save_checkpoint_ts`."""
  to_load = path

  try:
    # When passing a general path (not a specific step), get the last available.
    with gfile.GFile(f"{path}-LAST", "r") as f:
      to_load = f"{path}-{f.read().strip()}"
  except Exception:  # Differs based on backend, so blanket catch. pylint:disable=broad-exception-caught
    pass

  return tsload(to_load, **tsload_kw)


def tsload(path, *, tree=None, shardings=None, regex=None):
  """Loads tensorstore-based array-tree from disk.

  If `tree` argument is provided, then array names to load and target structure
  is derived from the tree. If `tree` is None, then array names to load are
  derived from array filenames on the disk, and, optionally, `regex` is applied
  to filter these names. The`tree` argument is then automatically derived from
  array names with `recover_tree` util.

  Arrays are loaded to CPU/TPU/GPU memory as specified by the `shardings`
  argument, which is a pytree of CPU/TPU/GPU shardings (can be mixed within a
  single pytree). `shardings` should a prefix tree of the `tree` argument. We
  automatically broadcast `shardings` to a full `tree`. For example, a user can
  specify `shardings=jax.sharding.SingleDeviceSharing(jax.devices('cpu')[0])`,
  which  will be broadcasted to a full tree.

  Args:
    path: a directory where the checkpoint arrays are stored.
    tree: a target pytree, which defines array names to load and the target tree
      structure. If tree is None, then `tree` is inferred from the names of
      arrays stored on the disk.
    shardings: a prefix pytree (with respect to `tree`) of the target shardings.
    regex: regex to filter array names from the disk, if `tree` is not provided.

  Returns:
    A pytree of loaded arrays that has the same structure as `shardings` arg.
  """
  if (tree is not None) and (regex is not None):
    raise ValueError("If tree is specified, regex filtering is not allowed.")

  if tree is None:
    # Some file-systems (gs://) list folders with a trailing /, get rid of it.
    path_names = set([p.rstrip("/").replace("~", "/")
                      for p in gfile.listdir(path)])
    regex = re.compile(regex) if regex is not None else re.compile(".*")
    path_names = [p for p in path_names if regex.match(p)]
    tree = recover_tree(path_names, [0] * len(path_names))

  names_and_vals, tree_def = tree_flatten_with_names(tree)
  names_to_load, _ = zip(*names_and_vals)

  if shardings is None:
    shardings = jax.sharding.SingleDeviceSharding(
        jax.local_devices(backend="cpu")[0]
    )
  shardings = list(jax.tree.leaves(tree_broadcast(shardings, tree)))

  names_to_load = [os.path.join(path, name.replace("/", "~"))
                   for name in names_to_load]
  specs = [array_serial.get_tensorstore_spec(n) for n in names_to_load]
  arrays = array_serial.run_deserialization(shardings, specs)
  return tree_def.unflatten(arrays)


def recover_tree(keys, values):
  """Recovers a tree as a nested dict from flat names and values.

  This function is useful to analyze checkpoints that are saved by our programs
  without need to access the exact source code of the experiment. In particular,
  it can be used to extract an reuse various subtrees of the scheckpoint, e.g.
  subtree of parameters.

  Args:
    keys: a list of keys, where '/' is used as separator between nodes.
    values: a list of leaf values.

  Returns:
    A nested tree-like dict.
  """
  tree = {}
  sub_trees = collections.defaultdict(list)
  for k, v in zip(keys, values):
    if "/" not in k:
      tree[k] = v
    else:
      k_left, k_right = k.split("/", 1)
      sub_trees[k_left].append((k_right, v))
  for k, kv_pairs in sub_trees.items():
    k_subtree, v_subtree = zip(*kv_pairs)
    tree[k] = recover_tree(k_subtree, v_subtree)
  return tree


def tree_flatten_with_names(tree):
  """Populates tree_flatten with leaf names.

  This function populates output of tree_flatten with leaf names, using a
  custom traversal that produces names is provided. The custom traversal does
  NOT have to traverse tree in the same order as jax, as we take care of
  automatically aligning jax' and custom traversals.

  Args:
    tree: python tree.

  Returns:
    A list of values with names: [(name, value), ...]
  """
  vals, tree_def = jax.tree.flatten(tree)

  # "Fake" token tree that is use to track jax internal tree traversal and
  # adjust our custom tree traversal to be compatible with it.
  tokens = range(len(vals))
  token_tree = tree_def.unflatten(tokens)
  val_names, perm = zip(*_traverse_with_names(token_tree))
  inv_perm = np.argsort(perm)

  # Custom traverasal should visit the same number of leaves.
  assert len(val_names) == len(vals)

  return [(val_names[i], v) for i, v in zip(inv_perm, vals)], tree_def


def _traverse_with_names(tree, with_inner_nodes=False):
  """Traverses nested dicts/dataclasses and emits (leaf_name, leaf_val)."""
  if dataclasses.is_dataclass(tree):
    tree = flax.serialization.to_state_dict(tree)
  # Don't output the non-leaf nodes. If the optimizer doesn't have a state
  # the tree leaves can be Nones which was interpreted as a leaf by this
  # function but not by the other functions (like jax.tree.map).
  if tree is None:
    return
  elif isinstance(tree, Mapping):
    keys = sorted(tree.keys())
    for key in keys:
      for path, v in _traverse_with_names(tree[key], with_inner_nodes):
        yield (key + "/" + path).rstrip("/"), v
    if with_inner_nodes:
      yield "", tree
  elif isinstance(tree, (list, tuple)):
    for idx in range(len(tree)):
      for path, v in _traverse_with_names(tree[idx], with_inner_nodes):
        yield (str(idx) + "/" + path).rstrip("/"), v
    if with_inner_nodes:
      yield "", tree
  else:
    yield "", tree


def tree_broadcast(prefix, target):
  """Broadcasts a prefix tree to a full tree.

  Input-output examples:
  1. prefix: {"x": 10, "y": 20}
     target: {"x": {"a": 1, "b": 2}, "y": 3}

     Result: {"x": {"a": 10, "b": 10}, "y": 20}

  2. prefix: 100
     target: {"x": {"a": 1, "b": 2}, "y": 3}

     Result: {"x": {"a": 100, "b": 100}, "y": 100}

  3. prefix: {"x": 10}
     target: {"x": {"a": 1, "b": 2}, "y": 3}

     Result: ValueError

  Args:
    prefix: prefix pytree.
    target: boradcast target for a prefix tree.

  Returns:
    prefix tree broadcasted to a target tree.
  """
  def _broadcast(leaf, subtree):
    return jax.tree.map(lambda _: leaf, subtree)
  return jax.tree.map(_broadcast, prefix, target)


# Learning Rate Schedule
# -----------------------------------------------------------------------------


def create_learning_rate_schedule(config):
  """Creates a warmup and cosine decay learning rate schedule:
  We take inspiration from Llama2's learning rate (LR) schedule, see https://arxiv.org/pdf/2307.09288.pdf section 2.2
  Learning rate schedule has either two or three parts:
  1) Linear warmup from 0 to [learning_rate] over steps 0 to [learning_rate_schedule_steps * warmup_steps_fraction]
  2) Cosine from [learning_rate] to [learning_rate * cosine_learning_rate_final_fraction] until learning_rate_schedule_steps
  3) Constant learning rate of 0 from learning_rate_schedule_steps to steps.
  The zero learning rate section can be used to more accurately measure the fully trained model's performance.
  """

  def make_cos_schedule(init_lr, final_lr, len_steps):
    def schedule(step):
      pct = (step) / len_steps
      a = 0.5 * (jnp.cos(jnp.pi * pct) + 1)
      lr = init_lr * a + final_lr * (1 - a)
      return lr

    return schedule

  lr = config.learning_rate
  cos_final_lr = lr * config.cosine_learning_rate_final_fraction

  warmup_steps = int(
      config.learning_rate_schedule_steps * config.warmup_steps_fraction
  )
  cos_steps = config.learning_rate_schedule_steps - warmup_steps
  constant_zero_steps = config.steps - config.learning_rate_schedule_steps

  warmup_schedule = optax.linear_schedule(
      init_value=0.0, end_value=lr, transition_steps=warmup_steps
  )
  cos_schedule = make_cos_schedule(lr, cos_final_lr, cos_steps)
  constant_schedule = optax.constant_schedule(0.0)

  pieces = [warmup_schedule, cos_schedule]
  boundaries = [
      warmup_steps,
      warmup_steps + cos_steps,
  ]

  if constant_zero_steps > 0:
    pieces.append(constant_schedule)
    boundaries.append(warmup_steps + cos_steps + constant_zero_steps)

  return optax.join_schedules(pieces, boundaries)


# Cross entropy implementation is taken from original T5X codebase:
# https://github.com/google-research/t5x/blob/ace831eea1e2742b4299cd1a9af7e4f302038351/t5x/losses.py#L25-L101
@jax.custom_vjp
def cross_entropy_with_logits(
    logits: jnp.ndarray, targets: jnp.ndarray, z_loss: float
) -> Tuple[jnp.ndarray, jnp.ndarray]:
  """Computes cross entropy loss with stable custom gradient.
  Computes a stabilized-gradient version of:
    -jnp.sum(targets * nn.log_softmax(logits), axis=-1)
  If z_loss > 0, then an auxiliary loss equal to z_loss*log(z)^2
  will be added to the cross entropy loss (z = softmax normalization constant).
  The two uses of z_loss are:
  1. To keep the logits from drifting too far from zero, which can cause
     unacceptable roundoff errors in bfloat16.
  2. To encourage the logits to be normalized log-probabilities.
  Args:
    logits: [batch, length, num_classes] float array.
    targets: categorical one-hot targets [batch, length, num_classes] float
      array.
    z_loss: coefficient for auxiliary z-loss loss term.
  Returns:
    tuple with the total loss and the z_loss, both
    float arrays with shape [batch, length].
  """
  logits_sum = jax.scipy.special.logsumexp(logits, axis=-1, keepdims=True)
  log_softmax = logits - logits_sum
  loss = -jnp.sum(targets * log_softmax, axis=-1)
  # Add auxiliary z-loss term.
  log_z = jnp.squeeze(logits_sum, axis=-1)
  total_z_loss = z_loss * jax.lax.square(log_z)
  loss += total_z_loss
  return loss, total_z_loss


def _cross_entropy_with_logits_fwd(
    logits: jnp.ndarray, targets: jnp.ndarray, z_loss: float = 0.0
) -> Tuple[
    Tuple[jnp.ndarray, jnp.ndarray],
    Tuple[
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
    ],
]:
  """Forward-mode of `cross_entropy_with_logits`."""
  max_logit = logits.max(axis=-1, keepdims=True)
  shifted = logits - max_logit
  exp_shifted = jnp.exp(shifted)
  sum_exp = jnp.sum(exp_shifted, axis=-1, keepdims=True)
  log_softmax = shifted - jnp.log(sum_exp)
  loss = -jnp.sum(targets * log_softmax, axis=-1)
  # Add auxiliary z-loss term.
  log_z = jnp.squeeze(jnp.log(sum_exp) + max_logit, axis=-1)
  total_z_loss = z_loss * jax.lax.square(log_z)
  loss += total_z_loss
  return (loss, total_z_loss), (
      logits,
      targets,
      z_loss,
      exp_shifted,
      sum_exp,  # pytype: disable=bad-return-type  #jax-ndarray
      log_softmax,
      log_z,
  )


def _cross_entropy_with_logits_bwd(
    res: Tuple[
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
    ],
    g: Tuple[jnp.ndarray, jnp.ndarray],
) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
  """Backward-mode of `cross_entropy_with_logits`."""
  g = g[0]  # Ignore z_loss component as that is only used for logging.
  logits, targets, z_loss, exp_shifted, sum_exp, log_softmax, log_z = res
  # z-loss term adds the (2 * z_loss * log_z) factor.
  deriv = (
      jnp.expand_dims(1 + 2 * z_loss * log_z, -1) * exp_shifted / sum_exp
      - targets
  )
  g_logits = jnp.expand_dims(g, axis=-1) * deriv
  g_targets = -jnp.expand_dims(g, axis=-1) * log_softmax
  return (
      jnp.asarray(g_logits, logits.dtype),
      jnp.asarray(g_targets, targets.dtype),
      jnp.array(0.0),
  )  # sets z-loss coeff gradient to 0


cross_entropy_with_logits.defvjp(
    _cross_entropy_with_logits_fwd, _cross_entropy_with_logits_bwd
)


def get_abstract_state(model, tx, config, rng, mesh, is_training=True):
  """Get a shaped abstraction of the state (including optimizer)"""
  init_state_partial = functools.partial(
      init_initial_state, model, tx, config, is_training
  )

  with nn_partitioning.axis_rules(config.logical_axis_rules):
    abstract_state = jax.eval_shape(init_state_partial, rng)

  state_logical_annotations = nn.get_partition_spec(abstract_state)

  state_mesh_shardings = nn.logical_to_mesh_sharding(
      state_logical_annotations, mesh, config.logical_axis_rules
  )

  abstract_sharded_state = jax.jit(
      init_state_partial, in_shardings=None, out_shardings=state_mesh_shardings
  ).eval_shape(rng)

  unboxed_abstract_sharded_state = unbox_logicallypartioned(
      abstract_sharded_state
  )
  # Initialization
  with mesh, nn_partitioning.axis_rules(config.logical_axis_rules):
    state_mesh_annotations = nn.logical_to_mesh(state_logical_annotations)
  return (
      unboxed_abstract_sharded_state,
      state_mesh_annotations,
      state_mesh_shardings,
  )


def get_kv_cache_annotations(model, config, rng, mesh):
  """Get a shaped abstraction of the state (including optimizer)"""

  def init_kv_cache(model, config):
    input_shape = (
        config.global_batch_size_to_load,
        config.max_prefill_predict_length,
    )

    model_vars = model.init(
        {"params": rng, "dropout": rng, "aqt": rng},
        jnp.ones(input_shape),
        jnp.ones(input_shape),
        model_mode=common_types.MODEL_MODE_PREFILL,
    )
    return model_vars["cache"]

  with nn_partitioning.axis_rules(config.logical_axis_rules):
    init_kv_cache_partial = functools.partial(init_kv_cache, model, config)
    abstract_state = jax.eval_shape(init_kv_cache_partial)
  state_logical_annotations = nn.get_partition_spec(abstract_state)
  with mesh, nn_partitioning.axis_rules(config.logical_axis_rules):
    state_mesh_annotations = nn.logical_to_mesh(state_logical_annotations)
  return state_mesh_annotations


def print_pytree_shape(print_str, ptree):
  print("\n")
  print(print_str)
  print(jax.tree_util.tree_map(lambda x: x.shape, ptree))


def print_model_vars(print_str, model_vars):
  for k in model_vars:
    print(f"{print_str} key{k}:")
    print(f"\t {model_vars[k]}")


def get_project():
  """Get project"""
  completed_command = subprocess.run(
      ["gcloud", "config", "get", "project"], check=True, capture_output=True
  )
  project_outputs = completed_command.stdout.decode().strip().split("\n")
  if len(project_outputs) < 1 or project_outputs[-1] == "":
    max_logging.log(
        "You must specify config.vertex_tensorboard_project or set 'gcloud config set project <project>'"
    )
    return None
  return project_outputs[-1]


def delete_pytree(p):
  def delete_leaf(leaf):
    if isinstance(leaf, jax.Array):
      leaf.delete()
    del leaf

  jax.tree_util.tree_map(delete_leaf, p)


def summarize_pytree_data(params, name="Params", raw=False):
  """Generate basic metrics of a given Pytree."""
  num_params, total_param_size, avg_param_size = summarize_size_from_pytree(
      params
  )
  if not raw:
    num_params_in_billions = num_params / 1e9
    total_param_size_in_gb = total_param_size / 1e9
    print(
        f"{name} stats: \n"
        f"\tTotal number of params: {num_params_in_billions:.3f} billion \n"
        f"\tTotal memory usage: {total_param_size_in_gb:.3f} GB \n"
        f"\tAvg size: {avg_param_size:.3f} bytes\n"
    )
  else:
    print(
        f"{name} stats: \n"
        f"\tTotal number of params: {num_params:.3f} \n"
        f"\tTotal memory usage: {total_param_size:.3f} bytes \n"
        f"\tAvg size: {avg_param_size:.3f} bytes\n"
    )
  return num_params, total_param_size, avg_param_size


def save_quantized_checkpoint_if_configured(config, params):
  assert config.quantization, 'quantization must be configured'
  if config.save_quantized_params_path:
    checkpointing.save_params_to_path(config.save_quantized_params_path, params)
  else:
    "Skipping saving quantized checkpoint as save_quantized_params_path is null."


def print_mem_stats(label:str):
  print(f'\nMemstats: {label}:')
  for d in jax.local_devices():
    stats = d.memory_stats()
    used = round(stats['bytes_in_use']/2**30, 2)
    limit = round(stats['bytes_limit']/2**30, 2)
    print(f"\tUsing (GB) {used} / {limit} ({used/limit:%}) on {d}")