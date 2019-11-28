# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import namedtuple, defaultdict
from distutils.util import strtobool
import itertools as it
import operator as op
import os

from absl import logging
import numpy as onp
import six
from six.moves import xrange

from ..config import flags
from .. import core
from .. import ad_util
from .. import tree_util
from .. import dtypes
from .. import linear_util as lu
from ..abstract_arrays import (ConcreteArray, ShapedArray, AbstractToken,
                               make_shaped_array, array_types, raise_to_shaped,
                               abstract_token, make_abstract_python_scalar)
from ..core import valid_jaxtype, Literal
from ..util import partial, partialmethod, cache, safe_map, prod, unzip2
from ..lib import xla_bridge as xb
from ..lib import xla_client as xc
from . import partial_eval as pe
from . import ad

FLAGS = flags.FLAGS
flags.DEFINE_bool('jax_debug_nans',
                  strtobool(os.getenv('JAX_DEBUG_NANS', "False")),
                  'Add nan checks to every operation.')
flags.DEFINE_bool('jax_log_compiles',
                  strtobool(os.getenv('JAX_LOG_COMPILES', "False")),
                  'Print a message each time a `jit` computation is compiled.')

def _map(f, *xs): return tuple(map(f, *xs))
def identity(x): return x


### lazy sub-language

LazyExpr = namedtuple('LazyExpr', ['input', 'shape', 'dims'])
LazyArrayVar = namedtuple('ArrayVar', [])
LazyIota = namedtuple('Iota', ['dtype', 'size'])
LazyEye = namedtuple('Eye', ['dtype', 'shape', 'offset'])
LazyTri = namedtuple('Tri', ['dtype', 'shape', 'offset'])

def lazy_array(shape):
  return LazyExpr(LazyArrayVar(), shape, tuple(range(len(shape))))

def lazy_iota(dtype, size):
  return LazyExpr(LazyIota(dtype, size), (size,), (0,))

def lazy_eye(dtype, shape, offset):
  assert len(shape) == 2
  return LazyExpr(LazyEye(dtype, shape, offset), shape, (0, 1))

def lazy_tri(dtype, shape, offset):
  assert len(shape) == 2
  return LazyExpr(LazyTri(dtype, shape, offset), shape, (0, 1))

def lazy_broadcast(lazy_expr, shape, broadcast_dimensions):
  new_dims = [None] * len(shape)
  for i, d in enumerate(broadcast_dimensions):
    new_dims[d] = lazy_expr.dims[i]
  return LazyExpr(lazy_expr.input, shape, tuple(new_dims))

def lazy_transpose(lazy_expr, perm):
  new_shape = tuple(lazy_expr.shape[i] for i in perm)
  new_dims = tuple(lazy_expr.dims[i] for i in perm)
  return LazyExpr(lazy_expr.input, new_shape, new_dims)

def lazy_reshape(lazy_expr, new_sizes):
  assert tuple(lazy_expr.shape) == tuple(d for d in new_sizes if d != 1)
  dims = iter(lazy_expr.dims)
  new_dims = tuple(None if d == 1 else next(dims) for d in new_sizes)
  return LazyExpr(lazy_expr.input, new_sizes, new_dims)

def eval_lazy_expr(lazy_expr, x):
  input_, shape, dims = lazy_expr

  # first create a starting ndarray from input_
  t = type(input_)
  if t is LazyArrayVar:
    assert x is not None and type(x) is onp.ndarray
  elif t is LazyIota:
    assert x is None
    x = onp.arange(input_.size, dtype=input_.dtype)
  elif t is LazyEye:
    assert x is None
    x = onp.eye(input_.size, dtype=input_.dtype, k=input_.offset)
  elif t is LazyTri:
    assert x is None
    N, M = input_.shape
    x = onp.tri(N, M, dtype=input_.dtype, k=input_.offset)
  else:
    assert False

  # then apply the reindexing operation
  perm = [d for d in dims if d is not None]
  if perm != list(range(len(perm))):
    x = onp.transpose(x, perm)
  if shape != x.shape:
    in_shape = [1 if d is None else s for d, s in zip(dims, shape)]
    x = onp.broadcast_to(onp.reshape(x, in_shape), shape)

  return x

def stage_lazy_expr(c, lazy_expr, x):
  if lazy_expr is None:
    return x

  input_, shape, dims = lazy_expr

  # first create a starting XlaOp from input_
  t = type(input_)
  if t is LazyArrayVar:
    assert x is not None
  elif t is LazyIota:
    assert x is None
    x = c.Iota(input_.dtype, input_.size)
  elif t is LazyEye:
    assert x is None
    size = input_.size
    bool_eye = c.Eq(c.Add(c.BroadcastedIota(onp.uint32, (size, size), 0),
                          c.Constant(onp.array(input_.offset, onp.uint32))),
                    c.BroadcastedIota(onp.uint32, (size, size), 1))
    x = c.ConvertElementType(bool_eye, xb.dtype_to_etype(input_.dtype))
  elif t is LazyTri:
    assert x is None
    N, M = input_.shape
    bool_tri = c.Ge(c.Add(c.BroadcastedIota(onp.uint32, (N, M), 0),
                          c.Constant(onp.array(input_.offset, onp.uint32))),
                  c.BroadcastedIota(onp.uint32, (N, M), 1))
    x = c.ConvertElementType(bool_tri, xb.dtype_to_etype(input_.dtype))
  else:
    assert False

  # then apply the operations encoded in reindex
  bcast_dims, perm = unzip2((i, d) for i, d in enumerate(dims) if d is not None)
  if tuple(perm) != tuple(range(len(perm))):
    x = c.Transpose(x, perm)
  if shape != c.GetShape(x).dimensions():
    x = c.BroadcastInDim(x, shape, bcast_dims)

  c.GetShape(x)  # force shape checking

  return x


ArgSpec = namedtuple("ArgSpec", ["aval", "lazy_expr", "xla_shape"])

def arg_spec(x):
  aval = abstractify(x)
  try:
    lazy_expr = x._lazy_expr
  except AttributeError:
    return ArgSpec(aval, None, aval_to_xla_shape(aval))
  else:
    if x.device_buffer is device_constant:
      xla_shape = None
    else:
      xla_shape = x.device_buffer.shape()
    return ArgSpec(aval, x._lazy_expr, xla_shape)


### handlers

xb.register_constant_handler(core.Unit, lambda c, *_: c.Tuple())

def aval_to_xla_shape(aval):
  try:
    return xla_shape_handlers[type(aval)](aval)
  except KeyError:
    raise TypeError("No xla_shape_handler for type: {}".format(type(aval)))
xla_shape_handlers = {}
xla_shape_handlers[core.AbstractUnit] = lambda _: xc.Shape.tuple_shape(())
xla_shape_handlers[ShapedArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)
xla_shape_handlers[ConcreteArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)

def aval_to_result_handler(aval):
  try:
    return xla_result_handlers[type(aval)](aval)
  except KeyError:
    raise TypeError("No xla_result_handler for type: {}".format(type(aval)))
xla_result_handlers = {}
xla_result_handlers[core.AbstractUnit] = lambda _: lambda _: core.unit
def array_result_handler(aval):
  return partial(DeviceArray, raise_to_shaped(aval), lazy_array(aval.shape))
xla_result_handlers[ShapedArray] = array_result_handler
xla_result_handlers[ConcreteArray] = array_result_handler

def device_put(x, device=None):
  x = canonicalize_dtype(x)
  try:
    return device_put_handlers[type(x)](x, device)
  except KeyError:
    raise TypeError("No device_put handler for type: {}".format(type(x)))

device_put_handlers = {}
device_put_handlers[core.Unit] = \
    lambda _, device: xc.Buffer.from_pyval(
        (), device, backend=xb.get_device_backend(device))
def _device_put_array(x, device):
  return xc.Buffer.from_pyval(x, device, backend=xb.get_device_backend(device))
for _t in array_types:
  device_put_handlers[_t] = _device_put_array

def _device_put_scalar(x, device):
  return xc.Buffer.from_pyval(dtypes.coerce_to_array(x), device,
                              backend=xb.get_device_backend(device))
for _t in dtypes.python_scalar_dtypes.keys():
  device_put_handlers[_t] = _device_put_array

# TODO(mattjj): try to remove this canonicalize_dtype stuff
def canonicalize_dtype(x):
  try:
    return canonicalize_dtype_handlers[type(x)](x)
  except KeyError:
    raise TypeError("No canonicalize_dtype handler for type: {}".format(type(x)))
canonicalize_dtype_handlers = {}
canonicalize_dtype_handlers[core.Unit] = identity
def _canonicalize_ndarray_dtype(x):
  return onp.asarray(x, dtypes.canonicalize_dtype(dtypes.result_type(x)))
for _t in array_types:
  canonicalize_dtype_handlers[_t] = _canonicalize_ndarray_dtype
def _canonicalize_python_scalar_dtype(x):
  return onp.asarray(
    x, dtypes.canonicalize_dtype(dtypes.python_scalar_dtypes[type(x)]))
for _t in dtypes.python_scalar_dtypes.keys():
  canonicalize_dtype_handlers[_t] = _canonicalize_python_scalar_dtype

def abstractify(x):
  try:
    return pytype_aval_mappings[type(x)](x)
  except KeyError:
    raise TypeError("No abstraction handler for type: {}".format(type(x)))
pytype_aval_mappings = {}
pytype_aval_mappings[core.Unit] = lambda _: core.abstract_unit
for _t in array_types:
  pytype_aval_mappings[_t] = make_shaped_array
for _t in dtypes.python_scalar_dtypes.keys():
  pytype_aval_mappings[_t] = make_abstract_python_scalar


### op-by-op execution

def apply_primitive(prim, *args, **params):
  """Impl rule that compiles and runs a single primitive 'prim' using XLA."""
  compiled_fun = xla_primitive_callable(prim, *map(arg_spec, args), **params)
  return compiled_fun(*args)

@cache()
def xla_primitive_callable(prim, *arg_specs, **params):
  if FLAGS.jax_log_compiles:
    print("Compiling {} for args {}.".format(prim.name, arg_specs))
  backend = params.get('backend', None)
  avals_in = [s.aval for s in arg_specs]
  aval_out = prim.abstract_eval(*avals_in, **params)
  if prim.multiple_results:
    handlers = tuple(map(aval_to_result_handler, aval_out))
    handle_result = lambda xs: tuple(h(x) for h, x in zip(handlers, xs.destructure()))
  else:
    handle_result = aval_to_result_handler(aval_out)
  built = _primitive_computation(prim, *arg_specs, **params)
  logging.debug(built.GetHloText())
  compiled = built.Compile(compile_options=xb.get_compile_options(),
                           backend=xb.get_backend(backend))
  return partial(_execute_compiled_primitive, prim, compiled, backend, handle_result)

@cache()
def primitive_computation(prim, *avals, **params):
  arg_specs = [ArgSpec(a, lazy_array(a.shape), aval_to_xla_shape(a)) for a in avals]
  return _primitive_computation(prim, *arg_specs, **params)

def _primitive_computation(prim, *arg_specs, **params):
  c = xb.make_computation_builder("primitive_computation_{}".format(prim.name))
  c.SetOpMetadata(xc.OpMetadata(op_type=prim.name, op_name=str(params)))
  backend = params.pop("backend", None)
  platform = xb.get_backend(backend).platform
  xla_args = _xla_callable_args(c, arg_specs, False)
  if prim in backend_specific_translations[platform]:
    rule = backend_specific_translations[platform][prim]
    rule(c, *xla_args, **params)  # return val set as a side-effect on c
  elif prim in translations:
    rule = translations[prim]
    rule(c, *xla_args, **params)  # return val set as a side-effect on c
  elif prim in reduction_translations:
    rule = reduction_translations[prim]
    rule(c, *xla_args, backend=backend, **params)  # return val set as a side-effect on c
  elif prim in initial_style_translations:
    rule = initial_style_translations[prim]
    rule(c, AxisEnv(), *xla_args,  backend=backend, **params)  # side-effect on c
  else:
    raise NotImplementedError("XLA translation rule for {} not found".format(prim))
  c.ClearOpMetadata()
  try:
    return c.Build()
  except RuntimeError as e:
    msg = (" ".join(map(str, e.args)) + "\n"
           "This is a bug in JAX's shape-checking rules; please report it!\n"
           "https://github.com/google/jax/issues\n")
    raise RuntimeError(msg)

def _execute_compiled_primitive(prim, compiled, backend, result_handler, *args):
  device, = compiled.local_devices()
  input_bufs = [device_put(x, device) for x in args
                if x is not token and not is_device_constant(x)]
  out_buf = compiled.Execute(input_bufs)
  if FLAGS.jax_debug_nans:
    check_nans(prim, out_buf.destructure() if prim.multiple_results else out_buf)
  return result_handler(out_buf)

def check_nans(prim, bufs):
  if prim.multiple_results:
    for buf in bufs:
      _check_nans(prim.name, buf.shape(), buf)
  else:
    _check_nans(prim.name, bufs.shape(), bufs)

def _check_nans(name, xla_shape, buf):
  if xla_shape.is_tuple():
    assert not xla_shape.tuple_shapes()
  else:
    if dtypes.issubdtype(xla_shape.element_type(), onp.floating):
      if onp.any(onp.isnan(buf.to_py())):
        msg = "invalid value (nan) encountered in {}"
        raise FloatingPointError(msg.format(name))

### compiling jaxprs

def prefetch(x):
  if isinstance(x, DeviceArray):
    x.copy_to_host_async()
  return x

def jaxpr_literals(jaxpr):
  return it.chain.from_iterable(eqn_literals(eqn) for eqn in jaxpr.eqns)

def eqn_literals(eqn):
  if eqn.bound_subjaxprs:
    (subjaxpr, _, _), = eqn.bound_subjaxprs
    for literal in jaxpr_literals(subjaxpr):
      yield literal
  if eqn.primitive in initial_style_translations:
    for param in eqn.params.values():
      if type(param) in (core.Jaxpr, core.TypedJaxpr):
        subjaxpr = param if type(param) is core.Jaxpr else param.jaxpr
        for literal in jaxpr_literals(subjaxpr):
          yield literal
  for v in eqn.invars:
    if type(v) is core.Literal:
      yield v.val

def jaxpr_subcomp(c, jaxpr, backend, axis_env, consts, freevars, *args):
  platform = xb.get_backend(backend).platform

  def read(v):
    if type(v) is Literal:
      return c.Constant(canonicalize_dtype(v.val))
    else:
      return env[v]

  def write(v, node):
    assert node is not None
    env[v] = node

  env = {}
  write(core.unitvar, c.Tuple())
  _map(write, jaxpr.constvars, consts)
  _map(write, jaxpr.freevars, freevars)
  _map(write, jaxpr.invars, args)
  for eqn in jaxpr.eqns:
    c.SetOpMetadata(xc.OpMetadata( op_type=eqn.primitive.name, op_name=str(eqn)))
    in_nodes = list(map(read, eqn.invars))
    if eqn.primitive in backend_specific_translations[platform]:
      rule = backend_specific_translations[platform][eqn.primitive]
      ans = rule(c, *in_nodes, **eqn.params)
    elif eqn.primitive in translations:
      ans = translations[eqn.primitive](c, *in_nodes, **eqn.params)
    elif eqn.primitive in reduction_translations:
      new_params = check_backend_params(eqn.params, backend)
      ans = reduction_translations[eqn.primitive](c, *in_nodes, backend=backend, **new_params)
    elif eqn.primitive in initial_style_translations:
      new_params = check_backend_params(eqn.params, backend)
      rule = initial_style_translations[eqn.primitive]
      ans = rule(c, axis_env, *in_nodes, backend=backend, **new_params)
    elif eqn.primitive in parallel_translations:
      new_params = check_backend_params(eqn.params, backend)
      replica_groups = axis_groups(axis_env, new_params['axis_name'])
      new_params = {k: new_params[k] for k in new_params if k != 'axis_name'}
      rule = parallel_translations[eqn.primitive]
      ans = rule(c, *in_nodes, replica_groups=replica_groups, backend=backend, **new_params)
    elif eqn.primitive in call_translations:
      new_params = check_backend_params(eqn.params, backend)
      (subjaxpr, const_bindings, freevar_bindings), = eqn.bound_subjaxprs
      const_nodes = _map(read, const_bindings)
      freevar_nodes = _map(read, freevar_bindings)
      rule = call_translations[eqn.primitive]
      ans = rule(c, subjaxpr, axis_env, const_nodes, freevar_nodes, in_nodes,
                 backend=backend, **new_params)
    else:
      msg = "XLA translation rule for primitive '{}' not found"
      raise NotImplementedError(msg.format(eqn.primitive.name))

    c.GetShape(ans)  # force xla to do shape error checking
    out_nodes = xla_destructure(c, ans) if eqn.primitive.multiple_results else [ans]
    c.ClearOpMetadata()
    _map(write, eqn.outvars, out_nodes)
  return _map(read, jaxpr.outvars)

def xla_destructure(c, ans):
  num_elements = len(c.GetShape(ans).tuple_shapes())
  return [c.GetTupleElement(ans, i) for i in range(num_elements)]

def check_backend_params(params, outer_backend):
  # For nested calls, the outermost call sets the backend for all inner calls;
  # it's an error if the inner call has a conflicting explicit backend spec.
  inner_backend = params.get('backend', None)
  if inner_backend and inner_backend != outer_backend:
    msg = (
      "Outer-jit backend specification {} must match explicit inner-jit "
      "backend specification {}.")
    raise ValueError(msg.format(outer_backend, inner_backend))
  return {k: params[k] for k in params if k != 'backend'}


class AxisEnv(object):
  def __init__(self, nreps=1, names=None, sizes=None, devices=None):
    self.nreps = nreps
    self.names = names if names else []
    self.sizes = sizes if sizes else []
    self.devices = devices

def extend_axis_env(env, name, size):
  return AxisEnv(env.nreps, env.names + [name], env.sizes + [size], env.devices)

def axis_read(axis_env, axis_name):
  return max(i for i, name in enumerate(axis_env.names) if name == axis_name)

def axis_groups(axis_env, name):
  if isinstance(name, (list, tuple)):
    mesh_axes = tuple(map(partial(axis_read, axis_env), name))
  else:
    mesh_axes = (axis_read(axis_env, name),)
  return _axis_groups(axis_env.nreps, axis_env.sizes, mesh_axes)

def _axis_groups(nrep, mesh_spec, mesh_axes):
  trailing_size, ragged = divmod(nrep, prod(mesh_spec))
  assert not ragged
  full_spec = list(mesh_spec) + [trailing_size]
  iota = onp.arange(prod(full_spec)).reshape(full_spec)
  groups = onp.reshape(
      onp.moveaxis(iota, mesh_axes, onp.arange(len(mesh_axes))),
      (prod(onp.take(full_spec, mesh_axes)), -1))
  return tuple(map(tuple, groups.T))

def jaxpr_replicas(jaxpr):
  return max(it.chain([1], (eqn_replicas(eqn) for eqn in jaxpr.eqns)))

def eqn_replicas(eqn):
  if eqn.bound_subjaxprs:
    (subjaxpr, _, _), = eqn.bound_subjaxprs
    return eqn.params.get('axis_size', 1) * jaxpr_replicas(subjaxpr)
  elif eqn.primitive in initial_style_translations:
    nums = (jaxpr_replicas(param if type(param) is core.Jaxpr else param.jaxpr)
            for param in eqn.params.values()
            if type(param) in (core.Jaxpr, core.TypedJaxpr))
    return max(it.chain([1], nums))
  else:
    return 1

def jaxpr_has_pmap(jaxpr):
  return any(eqn_has_pmap(eqn) for eqn in jaxpr.eqns)

def eqn_has_pmap(eqn):
  if eqn.bound_subjaxprs:
    (subjaxpr, _, _), = eqn.bound_subjaxprs
    return jaxpr_has_pmap(subjaxpr)
  elif eqn.primitive in initial_style_translations:
    return any(jaxpr_has_pmap(param if type(param) is core.Jaxpr else param.jaxpr)
               for param in eqn.params.values()
               if type(param) in (core.Jaxpr, core.TypedJaxpr))
  else:
    return 'pmap' in eqn.primitive.name


### xla_call underlying jit

def _xla_call_impl(fun, *args, **params):
  device = params['device']
  backend = params.get('backend', None)
  compiled_fun = _xla_callable(fun, device, backend, *map(arg_spec, args))
  try:
    return compiled_fun(*args)
  except FloatingPointError:
    print("Invalid value encountered in the output of a jit function. "
          "Calling the de-optimized version.")
    return fun.call_wrapped(*args)  # probably won't return

@lu.cache
def _xla_callable(fun, device, backend, *arg_specs):
  log_priority = logging.WARNING if FLAGS.jax_log_compiles else logging.DEBUG
  logging.log(log_priority,
              "Compiling {} for args {}.".format(fun.__name__, arg_specs))

  pvals = [pe.PartialVal((s.aval, core.unit)) for s in arg_specs]
  with core.new_master(pe.JaxprTrace, True) as master:
    jaxpr, (pvals, consts, env) = pe.trace_to_subjaxpr(fun, master, False).call_wrapped(pvals)
    assert not env  # no subtraces here
    del master, env
  _map(prefetch, it.chain(consts, jaxpr_literals(jaxpr)))

  nreps = jaxpr_replicas(jaxpr)
  if nreps > xb.device_count(backend):
    msg = ("compiling computation that requires {} replicas, but only {} XLA "
            "devices are available")
    raise ValueError(msg.format(nreps, xb.device_count(backend)))
  axis_env = AxisEnv(nreps, [], [])

  if xb.host_count() > 1 and (nreps > 1 or jaxpr_has_pmap(jaxpr)):
    raise NotImplementedError(
        "jit of multi-host pmap not implemented (and jit-of-pmap can cause "
        "extra data movement anyway, so maybe you don't want it after all).")

  tuple_args = len(arg_specs) > 100  # pass long arg lists as tuple for TPU

  c = xb.make_computation_builder("jit_{}".format(fun.__name__))
  xla_consts = _map(c.Constant, consts)
  xla_args = _xla_callable_args(c, arg_specs, tuple_args)
  out_nodes = jaxpr_subcomp(c, jaxpr, backend, axis_env, xla_consts, (), *xla_args)
  built = c.Build(c.Tuple(*out_nodes))

  if device is not None and nreps > 1:
    raise ValueError("can't specify device assignment for jit-of-pmap")
  options = xb.get_compile_options(
      num_replicas=nreps, device_assignment=(device.id,) if device else None)
  compiled = built.Compile(compile_options=options, backend=xb.get_backend(backend))

  result_handlers = tuple(map(_pval_to_result_handler, pvals))
  if nreps == 1:
    return partial(_execute_compiled, compiled, backend, result_handlers, tuple_args)
  else:
    return partial(_execute_replicated, compiled, backend, result_handlers, tuple_args)

def _xla_callable_args(c, arg_specs, tuple_args):
  if not tuple_args:
    raw_args = (c.ParameterWithShape(s.xla_shape) for s in arg_specs
                if s.aval is not abstract_token and s.xla_shape is not None)
  else:
    elt_shapes = [s.xla_shape for s in arg_specs
                  if s.aval is not abstract_token and s.xla_shape is not None]
    tuple_param = c.ParameterWithShape(xc.Shape.tuple_shape(elt_shapes))
    raw_args = iter(xla_destructure(c, tuple_param))
  xla_args = [stage_lazy_expr(c, s.lazy_expr, s.xla_shape and next(raw_args))
              if s.aval is not abstract_token else c.CreateToken()
              for s in arg_specs]
  assert next(raw_args, None) is None
  return xla_args

def _pval_to_result_handler(pval):
  pv, const = pval
  if pv is None:
    return lambda _: const
  else:
    return aval_to_result_handler(pv)

def _execute_compiled(compiled, backend, handlers, tuple_args, *args):
  device, = compiled.local_devices()
  input_bufs = [device_put(x, device) for x in args
                if x is not token and not is_device_constant(x)]
  if tuple_args:
    input_bufs = [make_tuple(input_bufs, device, backend)]
  out_bufs = compiled.Execute(input_bufs).destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_bufs)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]

def _execute_replicated(compiled, backend, handlers, tuple_args, *args):
  input_bufs = [
      [device_put(x, device) for x in args if x is not token]
      for device in compiled.local_devices()]
  if tuple_args:
    input_bufs = [[make_tuple(bufs, device, backend)] for bufs, device in
                  zip(input_bufs, compiled.local_devices())]
  out_bufs = compiled.ExecutePerReplica(input_bufs)[0].destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_bufs)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]

def make_tuple(bufs, device, backend):
  return xb.get_backend(backend).make_tuple(bufs, device)


xla_call_p = core.Primitive('xla_call')
xla_call_p.multiple_results = True
xla_call = partial(core.call_bind, xla_call_p)
xla_call_p.def_custom_bind(xla_call)
xla_call_p.def_impl(_xla_call_impl)

def _xla_call_translation_rule(c, jaxpr, axis_env, const_nodes, freevar_nodes,
                               in_nodes, device=None, backend=None):
  del device  # Ignored.
  subc = xb.make_computation_builder("jaxpr_subcomputation")  # TODO(mattjj): name
  consts = [subc.ParameterWithShape(c.GetShape(n)) for n in const_nodes]
  freevars = [subc.ParameterWithShape(c.GetShape(n)) for n in freevar_nodes]
  args = [subc.ParameterWithShape(c.GetShape(n)) for n in in_nodes]
  out_nodes = jaxpr_subcomp(subc, jaxpr, backend, axis_env, consts, freevars, *args)
  subc = subc.Build(subc.Tuple(*out_nodes))
  return c.Call(subc, list(const_nodes) + list(freevar_nodes) + list(in_nodes))
ad.primitive_transposes[xla_call_p] = partial(ad.call_transpose, xla_call_p)


### translation tables

translations = {}
reduction_translations = {}
parallel_translations = {}
initial_style_translations = {}
call_translations = {}
backend_specific_translations = defaultdict(dict)

translations[core.identity_p] = lambda c, x: x
call_translations[xla_call_p] = _xla_call_translation_rule

def zeros_like_translation_rule(c, x):
  shape = c.GetShape(x)
  if shape.is_tuple():
    assert not shape.tuple_shapes()
    return c.Tuple()
  else:
    zero = c.Constant(onp.array(0, shape.element_type()))
    return c.Broadcast(zero, shape.dimensions())
translations[ad_util.zeros_like_p] = zeros_like_translation_rule

def add_jaxvals_translation_rule(c, x, y):
  shape = c.GetShape(x)
  if shape.is_tuple():
    assert not shape.tuple_shapes()
    return x
  else:
    return c.Add(x, y)
translations[ad_util.add_jaxvals_p] = add_jaxvals_translation_rule

def lower_fun(fun, instantiate=False, initial_style=False):
  """Build a translation rule for a traceable function."""
  def f(c, *args, **params):
    backend = params.pop('backend', None)
    if initial_style:
      axis_env, xla_args = args[0], args[1:]
    else:
      axis_env, xla_args = AxisEnv(), args
    xla_shapes = tuple(map(c.GetShape, xla_args))
    avals = map(_aval_from_xla_shape, xla_shapes)
    pvals = [pe.PartialVal((a, core.unit)) for a in avals]
    jaxpr, _, consts = pe.trace_to_jaxpr(
        lu.wrap_init(fun, params), pvals, instantiate=True)
    consts = _map(c.Constant, consts)
    outs = jaxpr_subcomp(c, jaxpr, backend, axis_env, consts, (), *xla_args)
    return c.Tuple(*outs)
  return f

def _aval_from_xla_shape(xla_shape):
  if xla_shape.is_tuple() and not xla_shape.tuple_shapes():
    return core.abstract_unit
  else:
    return ShapedArray(xla_shape.dimensions(), xla_shape.element_type())


### device-persistent data

class Token(object): pass
token = Token()

pytype_aval_mappings[Token] = lambda _: abstract_token
core.pytype_aval_mappings[Token] = lambda _: abstract_token
xla_shape_handlers[AbstractToken] = lambda _: xc.Shape.token_shape()
xla_result_handlers[AbstractToken] = lambda _: lambda _: token
canonicalize_dtype_handlers[Token] = identity


class DeviceValue(object):
  """A DeviceValue represents a value backed by device memory."""
  __slots__ = ["aval", "device_buffer", "__weakref__"]

  def _check_if_deleted(self):
    if self.device_buffer is deleted_buffer:
      raise ValueError("DeviceValue has been deleted.")

  def block_until_ready(self):
    """Blocks the caller until the buffer's value has been computed on device.

    This method is mostly useful for timing microbenchmarks that wish to
    time how long a computation takes, without transferring the result back
    to the host.

    Returns the buffer object (`self`).
    """
    self._check_if_deleted()
    self.device_buffer.block_host_until_ready()
    return self

class DeletedBuffer(object): pass
deleted_buffer = DeletedBuffer()

class DeviceConstant(object): pass
device_constant = DeviceConstant()

def is_device_constant(x):
  return type(x) is DeviceArray and x.device_buffer is device_constant

def _forward_method(attrname, self, fun, *args):
  return fun(getattr(self, attrname), *args)
_forward_to_value = partial(_forward_method, "_value")

class DeviceArray(DeviceValue):
  """A DeviceArray is an ndarray backed by a single device memory buffer."""
  # We don't subclass ndarray because that would open up a host of issues,
  # but lax_numpy.py overrides isinstance behavior and attaches ndarray methods.
  __slots__ = ["_npy_value", "_lazy_expr"]
  __array_priority__ = 100

  def __init__(self, aval, lazy_expr, device_buffer):
    self.aval = aval
    self._lazy_expr = lazy_expr
    self.device_buffer = device_buffer
    self._npy_value = None
    if not core.skip_checks:
      assert type(aval) is ShapedArray
      npy_value = self._value
      assert npy_value.dtype == aval.dtype and npy_value.shape == aval.shape

  @property
  def _value(self):
    self._check_if_deleted()
    if self._npy_value is None:
      if self.device_buffer is device_constant:
        self._npy_value = eval_lazy_expr(self._lazy_expr, None)
      else:
        self.device_buffer = force(self).device_buffer
        self._lazy_expr = lazy_array(self.aval.shape)
        self._npy_value = self.device_buffer.to_py()
      self._npy_value.flags.writeable = False
    return self._npy_value

  @property
  def shape(self):
    return self.aval.shape

  @property
  def dtype(self):
    return self.aval.dtype

  @property
  def size(self):
    return prod(self.aval.shape)

  @property
  def ndim(self):
    return len(self.aval.shape)

  def copy(self):
    """Returns an ndarray (backed by host memory, not device memory)."""
    return onp.asarray(self)

  def copy_to_host_async(self):
    """Requests a copy of the buffer to the host."""
    self._check_if_deleted()
    if self._npy_value is None and self.device_buffer is not device_constant:
      self.device_buffer.copy_to_host_async()

  def delete(self):
    """Deletes the device array and any cached copy on the host.

    It is an error to access the contents of a `DeviceArray` after it has
    been deleted.

    Use of this method is optional; device buffers will be reclaimed
    automatically by Python when a DeviceArray object is garbage collected.
    However, it is sometimes useful to have more explicit control over the
    time of deletion.
    """
    self.device_buffer.delete()
    self.device_buffer = deleted_buffer
    self._npy_value = None

  def __repr__(self):
    line_width = onp.get_printoptions()['linewidth']
    prefix = '{}('.format(self.__class__.__name__)
    s = onp.array2string(self._value, prefix=prefix, suffix=',',
                         separator=', ', max_line_width=line_width)
    dtype_str = 'dtype={})'.format(self.dtype.name)
    last_line_len = len(s) - s.rfind('\n') + 1
    sep = ' '
    if last_line_len + len(dtype_str) + 1 > line_width:
      sep = ' ' * len(prefix)
    return "{}{},{}{}".format(prefix, s, sep, dtype_str)

  def item(self):
    if dtypes.issubdtype(self.dtype, onp.complexfloating):
      return complex(self)
    elif dtypes.issubdtype(self.dtype, onp.floating):
      return float(self)
    elif dtypes.issubdtype(self.dtype, onp.integer):
      return int(self)
    elif dtypes.issubdtype(self.dtype, onp.bool_):
      return bool(self)
    else:
      raise TypeError(self.dtype)

  def __len__(self):
    try:
      return self.aval.shape[0]
    except IndexError:
      raise TypeError("len() of unsized object")  # same as numpy error

  def __iter__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")  # same as numpy error
    else:
      return self._value.__iter__()

  def __reversed__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")
    else:
      return reversed(self._value)

  def __format__(self, format_spec):
    # Simulates behavior of https://github.com/numpy/numpy/pull/9883
    if self.ndim == 0:
      return format(self._value[()], format_spec)
    else:
      return format(self._value, format_spec)

  def __array__(self, dtype=None, context=None):
    return onp.asarray(self._value, dtype=dtype)

  __str__ = partialmethod(_forward_to_value, str)
  __bool__ = __nonzero__ = partialmethod(_forward_to_value, bool)
  __float__ = partialmethod(_forward_to_value, float)
  __int__ = partialmethod(_forward_to_value, int)
  if six.PY2:
    __long__ = partialmethod(_forward_to_value, long)  # noqa: F821
  __complex__ = partialmethod(_forward_to_value, complex)
  __hex__ = partialmethod(_forward_to_value, hex)
  __oct__ = partialmethod(_forward_to_value, oct)
  __index__ = partialmethod(_forward_to_value, op.index)

  # pickle saves and loads just like an ndarray
  __reduce__ = partialmethod(_forward_to_value, op.methodcaller("__reduce__"))

  # clobbered when jax.numpy is imported, but useful in tests
  def __eq__(self, other): return self._value == other

  def __hash__(self):
    raise TypeError("JAX DeviceArray, like numpy.ndarray, is not hashable.")

core.literalable_types.add(DeviceArray)
core.pytype_aval_mappings[DeviceArray] = ConcreteArray
pytype_aval_mappings[DeviceArray] = lambda x: x.aval
canonicalize_dtype_handlers[DeviceArray] = identity

def _device_array_constant_handler(c, val, canonicalize_types=True):
  if val.device_buffer is device_constant:
    return stage_lazy_expr(c, val._lazy_expr, None)
  else:
    base_val = c.Constant(val._value)
    return stage_lazy_expr(c, val._lazy_expr, base_val)
xb.register_constant_handler(DeviceArray, _device_array_constant_handler)

def _device_put_device_array(x, device):
  # TODO(skye): we're assuming the DeviceBuffers without "platform" are
  # XrtBuffers. Figure out a less risky way to deal with XrtBuffers.
  if (not hasattr(x.device_buffer, "platform") or
      xb.get_device_backend(device).platform == x.device_buffer.platform()):
    if device is None or x.device_buffer.device() == device:
      return x.device_buffer
    else:
      return x.device_buffer.copy_to_device(device)
  else:
   # Buffers from different XLA backends are passed through the host.
   return xc.Buffer.from_pyval(x, device, backend=xb.get_device_backend(device))
device_put_handlers[DeviceArray] = _device_put_device_array


def _device_put_impl(x, device=None):
  if type(x) is DeviceArray:
    return x

  try:
    a = abstractify(x)
  except TypeError:
    raise TypeError("Argument '{}' of type {} is not a valid JAX type"
                    .format(x, type(x)))
  handler = aval_to_result_handler(a)
  out = handler(device_put(x, device))

  # minor optimization: avoid round-tripping scalars by saving their numpy value
  if onp.isscalar(x):
    assert type(out) is DeviceArray
    out._npy_value = onp.array(x, dtype=a.dtype)
  return out

device_put_p = core.Primitive('device_put')
device_put_p.def_impl(_device_put_impl)
pe.custom_partial_eval_rules[device_put_p] = lambda trace, x, **params: x
ad.deflinear(device_put_p, lambda cotangent, **kwargs: [cotangent])


# To force a DeviceArray to be materialized, we just apply an identity primitive
def force(x):
  if type(x) is not DeviceArray:
    return x
  lexpr = x._lazy_expr
  if (type(lexpr.input) is LazyArrayVar and lexpr.dims == tuple(range(x.ndim))):
    return x  # trivial lazy expr
  else:
    return apply_primitive(force_p, x, aval=x.aval)

force_p = core.Primitive('force')
force_p.def_abstract_eval(lambda *args, **kwargs: kwargs['aval'])
translations[force_p] = lambda c, x, aval: x
