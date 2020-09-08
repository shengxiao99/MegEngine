import collections
import contextlib
import functools
import itertools
import json
import typing
import warnings
import weakref

import numpy as np

from ..core._imperative_rt import GraphProfiler
from ..core._imperative_rt.ops import OprAttr
from ..core.ops.special import Const
from ..core.tensor import megbrain_graph as G
from ..core.tensor.core import OpBase, TensorBase, TensorWrapperBase, apply
from ..core.tensor.raw_tensor import OpDef, RawTensor, as_raw_tensor
from ..core.tensor.tensor import Tensor
from .sublinear_memory_config import SublinearMemoryConfig


class TraceMismatchError(RuntimeError):
    pass


active_trace = None
skip_tracing = False


@contextlib.contextmanager
def exclude_from_trace():
    global skip_tracing
    if skip_tracing:
        yield
        return
    try:
        skip_tracing = True
        if active_trace is not None:
            active_trace._begin_excluded_region()
        yield
    finally:
        skip_tracing = False


class TensorInfo:
    __slots__ = (
        # collected attributes
        "external",
        "exported",
        "data_read",
        "shape_read",
        "value_read",
        "device",
        "dtype",
        "shape",
        "bound_data",
        # resources for execution
        "varnode",
        "data_setter",
        "shape_reader",
        "value_reader",
        "data_reader",
    )

    def __init__(self):
        self.exported = None
        self.data_read = None
        self.shape_read = None
        self.value_read = None
        self.bound_data = None

        self.data_setter = None
        self.shape_reader = None
        self.value_reader = None
        self.data_reader = None


class trace:
    def __new__(cls, *args, **kwargs):
        if not args:
            return functools.partial(cls, **kwargs)
        return super().__new__(cls)

    def __init__(
        self,
        function,
        symbolic=False,
        capture_as_const=False,
        sublinear_memory_config: SublinearMemoryConfig = None,
        profiling: bool = False,
    ):
        self.__wrapped__ = function
        self._symbolic = symbolic
        self._capture_as_const = capture_as_const
        self._sublinear_memory_config = sublinear_memory_config
        self._profiling = profiling
        self._profiler = None

        self._untraced = True
        self._tinfo = []  # handle -> TensorInfo
        self._seq = []
        self._pc = 0
        self._graph = None
        self._need_reset_nodes = None
        self._lazy_eval_graph = None
        self._lazy_eval_tensors = weakref.WeakSet()
        self._active_tensors = weakref.WeakSet()
        self._tensor_remaps = None
        self._inputs_to_restore = None
        self._arg_bindings = None
        self._kwarg_bindings = None
        self._output_bindings = None
        self._output_names = None

    def _new_handle(self):
        handle = len(self._tinfo)
        info = TensorInfo()
        self._tinfo.append(info)
        return handle, info

    def _apply_op(self, op, args):
        assert not self._untraced
        # check against trace
        if self._pc >= len(self._seq):
            raise TraceMismatchError("trace should end here, but more op observed")
        record = self._seq[self._pc]
        op_, ihandles, ohandles = record
        if op != op_:
            # FIXME: will be removed once better rng implementation is done
            if isinstance(op, OprAttr) and (
                op.type in ("UniformRNG", "GaussianRNG") and op.type == op_.type
            ):
                if op.param[8:] != op_.param[8:]:
                    raise TraceMismatchError("op different from last time")
            else:
                raise TraceMismatchError("op different from last time")
        if len(ihandles) != len(args):
            raise TraceMismatchError("op input size different from last time")

        for h, x in zip(ihandles, args):
            info = self._tinfo[h]
            if info.external:
                if (
                    x.__class__ is CompiledTensorProxy
                    and not self._tinfo[x._CompiledTensorProxy__handle].exported
                ):
                    raise TraceMismatchError(
                        "failed to capture: input was an external tensor "
                        "last time, got an internal tensor this time"
                    )
                if info.bound_data:
                    if x.__class__ is CompiledTensorProxy:
                        raise TraceMismatchError(
                            "const capture violated: was an external tensor "
                            "last time, got an internal tensor this time"
                        )
                    if x._handle != info.bound_data._handle:
                        if not np.array_equal(
                            x.numpy(), info.bound_data.numpy(), equal_nan=True
                        ):
                            raise TraceMismatchError(
                                "const capture violated: got "
                                "a different tensor this time"
                            )
                else:
                    if info.dtype != x.dtype:
                        raise TraceMismatchError(
                            "failed to capture: different dtype from last time"
                        )
                    if info.device != x.device:
                        raise TraceMismatchError(
                            "failed to capture: different device from last time"
                        )
                    info.data_setter.set_value(x._dev_tensor())
            else:
                if x.__class__ is not CompiledTensorProxy:
                    if x not in self._tensor_remaps:
                        raise TraceMismatchError(
                            "unexpected capture: trying to use an external tensor as "
                            "input, but that input was an internal tensor last time"
                        )
                    else:
                        x = self._tensor_remaps[x]
                if x._CompiledTensorProxy__handle != h:
                    raise TraceMismatchError(
                        "mis-wiring: input edge to an data flow "
                        "graph node is different from last time"
                    )

        self._pc += 1
        outputs = tuple([CompiledTensorProxy(h) for h in ohandles])
        self._active_tensors.update(outputs)
        return outputs

    def _record_op(self, op, inputs, outputs):
        if skip_tracing:
            for x in inputs:
                h = getattr(x, "_TraceMixin__handle", None)
                if h is not None:
                    self._tinfo[h].data_read = True
            return

        ihandles = []
        for x in inputs:
            h = getattr(x, "_TraceMixin__handle", None)
            if h is None or (not self._capture_as_const and self._tinfo[h].exported):
                h, info = self._new_handle()
                info.external = True
                info.device = x.device
                info.dtype = x.dtype
                info.shape = x.shape
                if self._capture_as_const:
                    info.bound_data = x

            ihandles.append(h)

        ohandles = []
        for x in outputs:
            h, info = self._new_handle()
            ohandles.append(h)
            info.external = False
            TraceMixin._TraceMixin__inject(x, h)

        self._seq.append((op, tuple(ihandles), tuple(ohandles)))
        self._active_tensors.update(outputs)

    def _record_const(self, op, outputs):
        pass

    @contextlib.contextmanager
    def _setup(self):
        global active_trace
        if active_trace:
            raise NotImplementedError("sorry, not implemented: nested trace")
        active_trace = self

        if self._untraced:
            apply.enable(apply_with_tracing)
            apply.enable(apply_const_with_tracing)
            if self._symbolic:
                apply.enable(apply_symbolic_mode)
                apply.enable(apply_const_symbolic_mode)
                self._lazy_eval_graph = G.Graph()
        else:
            apply.enable(apply_compiled_mode)
            if self._graph is None:
                self._compile()
            self._graph.execute()

        yield

        escaped_tensors = tuple(self._active_tensors)
        self._active_tensors.clear()

        if self._untraced:
            for x in escaped_tensors:
                info = self._tinfo[x._TraceMixin__handle]
                info.data_read = True
                x._TraceMixin__restore()
            if self._inputs_to_restore:
                for x in self._inputs_to_restore:
                    x._TraceMixin__restore()
            if self._symbolic:
                # eval lazy eval tensors
                lazy_eval_tensors = tuple(self._lazy_eval_tensors)
                if lazy_eval_tensors:
                    readers = [
                        G.OutputNode(x._LazyEvalTensor__varnode).outputs[0]
                        for x in lazy_eval_tensors
                    ]
                    self._apply_graph_options(self._lazy_eval_graph)
                    self._lazy_eval_graph.compile(*readers)
                    self._lazy_eval_graph()
                    for r, x in zip(readers, lazy_eval_tensors):
                        assign_raw_tensor(x, as_raw_tensor(r.op.get_value()))
                    self._lazy_eval_graph = None
                    self._lazy_eval_tensors = None
            self._untraced = False
        else:
            if self._pc != len(self._seq):
                raise TraceMismatchError("premature end")
            for x in escaped_tensors:
                assign_raw_tensor(x, as_raw_tensor(x._dev_tensor()))
            self._graph.wait()
            self._reset_exec_env()
            self._pc = 0

        self._tensor_remaps = None
        apply.disable(apply_with_tracing)
        apply.disable(apply_const_with_tracing)
        apply.disable(apply_symbolic_mode)
        apply.disable(apply_const_symbolic_mode)
        apply.disable(apply_compiled_mode)
        active_trace = None

    def _begin_excluded_region(self):
        if self._capture_as_const:
            raise RuntimeError(
                "exclude_from_trace cannot be used with capture_as_const"
            )
        if self._untraced:
            # conditionally reading a compiled tensor in excluded region
            # is permitted, so we have to assume every tensor might be read
            for x in self._active_tensors:
                info = self._tinfo[x._TraceMixin__handle]
                info.exported = True
                info.data_read = True

    def _apply_graph_options(self, graph):

        # sublinear
        if self._sublinear_memory_config is not None:
            graph.options.enable_sublinear_memory_opt = True
            sublinear_config = graph.options.sublinear_mem_config
            sublinear_config.lb_memory = self._sublinear_memory_config.lb_memory
            sublinear_config.genetic_nr_iter = (
                self._sublinear_memory_config.genetic_nr_iter
            )
            sublinear_config.genetic_pool_size = (
                self._sublinear_memory_config.genetic_pool_size
            )
            sublinear_config.thresh_nr_try = self._sublinear_memory_config.thresh_nr_try
            sublinear_config.num_worker = self._sublinear_memory_config.num_worker
        if self._profiling:
            self._profiler = GraphProfiler(graph)

    def _compile(self):
        graph = self._graph = G.Graph()
        graph.options.no_force_inplace = True
        self._apply_graph_options(graph)
        # graph.options.graph_opt_level = 0
        need_reset_nodes = self._need_reset_nodes = []
        # links enforce ordering of I/O nodes
        links = ()

        if self._capture_as_const:
            for h in itertools.chain(self._arg_bindings, self._kwarg_bindings.values()):
                info = self._tinfo[h]
                opnode = info.data_setter = G.InputNode(
                    device=info.device, dtype=info.dtype, shape=info.shape, graph=graph
                )
                need_reset_nodes.append(opnode)
                info.varnode = opnode.outputs[0]
                links += opnode.outputs[1:]

        for op, ihandles, ohandles in self._seq:
            ivars = []
            readers = []
            for h in ihandles:
                info = self._tinfo[h]
                if not hasattr(info, "varnode"):
                    assert info.external
                    if info.bound_data:
                        info.varnode = graph.make_const(info.bound_data._dev_tensor())
                    else:
                        opnode = info.data_setter = G.InputNode(
                            *links,
                            device=info.device,
                            dtype=info.dtype,
                            shape=info.shape,
                            graph=graph,
                        )
                        need_reset_nodes.append(opnode)
                        info.varnode, *links = opnode.outputs

                ivars.append(info.varnode)
            ovars = apply(op, *ivars)
            assert len(ovars) == len(ohandles)
            for h, v in zip(ohandles, ovars):
                info = self._tinfo[h]
                info.varnode = v

                def add_reader(opnode):
                    nonlocal links
                    need_reset_nodes.append(opnode)
                    readers.append(opnode.outputs[0])
                    links = opnode.outputs

                if info.data_read:
                    # Shape can be obtained from data so doesn't need its own
                    # output node. On the other hand, value is read separately
                    # to leverage eager h2d copy
                    info.shape_read = False
                    opnode = info.data_reader = G.OutputNode(v, *links)
                    add_reader(opnode)
                if info.value_read:
                    opnode = info.value_reader = G.ValueOutputNode(v, *links)
                    add_reader(opnode)
                if info.shape_read:
                    opnode = info.shape_reader = G.AttrOutputNode(v, *links)
                    add_reader(opnode)

        graph.compile(*readers)

    def _reset_exec_env(self):
        for opnode in self._need_reset_nodes:
            opnode.reset()

    def _require_shape(self, handle):
        info = self._tinfo[handle]
        info.shape_read = True

    def _require_value(self, handle):
        info = self._tinfo[handle]
        info.value_read = True

    def _require_data(self, handle):
        info = self._tinfo[handle]
        info.data_read = True

    def __call__(self, *args, **kwargs):
        with self._setup():
            if self._capture_as_const:
                self._process_inputs(*args, **kwargs)
            outputs = self.__wrapped__(*args, **kwargs)
            if self._capture_as_const:
                self._process_outputs(outputs)
            return outputs

    def dump(self, file, *, arg_names=None, output_names=None):
        if not self._capture_as_const:
            raise ValueError(
                "you must specify capture_as_const=True at __init__ to use dump"
            )
        if self._untraced:
            raise RuntimeError("should run at least once before calling dump")
        if self._output_names and output_names:
            raise TypeError(
                "cannot specify output_names when output is already in dict format"
            )
        if output_names and not isinstance(output_names, collections.Sequence):
            output_names = (output_names,)
        if output_names and len(output_names) != len(self._output_bindings):
            raise ValueError("wrong number of output_names")
        if arg_names and not isinstance(arg_names, collections.Sequence):
            arg_names = (arg_names,)
        if arg_names and len(arg_names) != len(self._arg_bindings):
            raise ValueError("wrong number of arg_names")
        output_names = output_names or self._output_names

        h2v = {}
        graph = G.Graph()

        for i, h in enumerate(self._arg_bindings):
            info = self._tinfo[h]
            h2v[h] = graph.make_h2d(
                dtype=info.dtype,
                device=info.device,
                shape=info.shape,
                name=arg_names[i] if arg_names else None,
            )
        for k, h in self._kwarg_bindings.items():
            info = self._tinfo[h]
            h2v[h] = graph.make_h2d(
                dtype=info.dtype, device=info.device, shape=info.shape, name=k
            )

        for op, ihandles, ohandles in self._seq:
            ivars = []
            for h in ihandles:
                info = self._tinfo[h]
                if h not in h2v:
                    assert info.external
                    assert info.bound_data
                    h2v[h] = graph.make_const(info.bound_data._dev_tensor())
                ivars.append(h2v[h])
            ovars = apply(op, *ivars)
            assert len(ovars) == len(ohandles)
            h2v.update(zip(ohandles, ovars))

        dest_vars = []
        for i, h in enumerate(self._output_bindings):
            v = h2v[h]
            if output_names:
                v.name = output_names[i]
            dest_vars.append(v)

        if isinstance(file, str):
            file = open(file, "wb")
        file.write(G.dump(*dest_vars))

    def _process_inputs(self, *args, **kwargs):
        if self._untraced:
            self._inputs_to_restore = []

            def record_input(x):
                if x is None:
                    return
                h, info = self._new_handle()
                info.external = False
                info.device = x.device
                info.dtype = x.dtype
                info.shape = x.shape
                TraceMixin._TraceMixin__inject(x, h)
                self._inputs_to_restore.append(x)
                return h

            self._arg_bindings = []
            for i, x in enumerate(args):
                x = find_raw_tensor(x)
                if x is None:
                    raise TypeError(
                        "positional arguments should all be tensor "
                        "but args[%d] cannot be recognized as one" % i
                    )
                self._arg_bindings.append(record_input(x))

            self._kwarg_bindings = {}
            for k, x in kwargs.items():
                x = find_raw_tensor(x)
                if x is not None:
                    self._kwarg_bindings[k] = record_input(x)
        else:
            if len(args) != len(self._arg_bindings):
                raise TraceMismatchError("positional argument length mismatch")

            self._tensor_remaps = {}

            for i, (h, x) in enumerate(zip(self._arg_bindings, args)):
                x = find_raw_tensor(x)
                if x is None:
                    raise TypeError(
                        "positional arguments should all be tensor "
                        "but args[%d] cannot be recognized as one" % i
                    )
                info = self._tinfo[h]
                if x.dtype != info.dtype:
                    raise TypeError("args[%d].dtype different from last time" % i)
                if x.device != info.device:
                    raise TypeError("args[%d].device different from last time" % i)
                info.data_setter.set_value(x._dev_tensor())
                self._tensor_remaps[x] = CompiledTensorProxy(h)

            kwargs_tensors = {}
            for k, x in kwargs.items():
                x = find_raw_tensor(x)
                if x is not None:
                    kwargs_tensors[k] = x
            if set(kwargs_tensors) != set(self._kwarg_bindings):
                too_many = set(kwargs_tensors) - set(self._kwarg_bindings)
                too_few = set(self._kwarg_bindings) - set(kwargs_tensors)
                if too_many:
                    raise TraceMismatchError(
                        "keyword arguments found to be tensor this time "
                        "but were non-tensor previously: %s" % " ".join(too_many)
                    )
                if too_few:
                    raise TraceMismatchError(
                        "keyword arguments found to be non-tensor this time "
                        "but were tensor previously: %s" % " ".join(too_few)
                    )
            for k, h in self._kwarg_bindings.items():
                x = kwargs_tensors[k]
                info = self._tinfo[h]
                if x.dtype != info.dtype:
                    raise TypeError("kwargs[%s].dtype different from last time" % k)
                if x.device != info.device:
                    raise TypeError("kwargs[%s].device different from last time" % k)
                info.data_setter.set_value(x._dev_tensor())
                self._tensor_remaps[x] = CompiledTensorProxy(h)

    def _process_outputs(self, outputs):
        output_names = None
        if isinstance(outputs, collections.Mapping):
            output_names, outputs = zip(*sorted(outputs.items()))
        elif not isinstance(outputs, collections.Sequence):
            outputs = (outputs,)

        if not self._untraced:
            if output_names != self._output_names:
                too_many = set(output_names) - set(self._output_names)
                too_few = set(self._output_names) - set(output_names)
                if too_many:
                    raise TraceMismatchError(
                        "output has more keys than last time: %s" % " ".join(too_many)
                    )
                if too_few:
                    raise TraceMismatchError(
                        "output has less keys than last time: %s" % " ".join(too_few)
                    )
            if len(outputs) != len(self._output_bindings):
                raise TraceMismatchError("output size differs from last time")
        else:
            self._output_names = output_names
            self._output_bindings = []

        for i, x in enumerate(outputs):
            x = find_raw_tensor(x)
            if x is None:
                raise TypeError("every item of return value should be tensor")
            if self._untraced:
                if not isinstance(x, TraceMixin):
                    raise RuntimeError("output is not computed from inputs")
                h = x._TraceMixin__handle
                self._output_bindings.append(h)
            else:
                if not isinstance(x, CompiledTensorProxy):
                    raise RuntimeError("output is not computed from inputs")
                h = x._CompiledTensorProxy__handle
                if h != self._output_bindings[i]:
                    raise TraceMismatchError(
                        "retval[%s] is a different tensor than last time"
                        % (output_names and output_names[i] or i)
                    )

    def get_profile(self):
        """
        Get profiling result for compiled trace.

        :return: a json compatible object.
        """
        if not self._profiler:
            raise RuntimeError("trace is not set with profiling=True")
        return json.loads(self._profiler.get())


class CompiledTensorProxy(RawTensor):
    """
    Duck-typed RawTensor
    """

    def __init__(self, handle):
        self.__handle = handle
        self.__info = active_trace._tinfo[handle]
        self.__shape = None
        self.__data = None
        self.__value = None

    @property
    def dtype(self):
        return self.__info.varnode.dtype

    @property
    def device(self):
        return self.__info.varnode.device

    @property
    def shape(self):
        if self.__shape is None:
            if self.__info.shape_read:
                self.__shape = self.__info.shape_reader.get_value().shape
            elif self.__info.data_read:
                self.__shape = self._dev_tensor().shape
            else:
                raise TraceMismatchError("shape of this tensor is not read in trace")
        return self.__shape

    def numpy(self):
        if self.__value is None:
            if self.__info.value_read:
                self.__value = self.__info.value_reader.get_value()
            elif self.__info.data_read:
                self.__value = self._dev_tensor().numpy()
            else:
                raise TraceMismatchError("value of this tensor is not read in trace")
        return self.__value

    def _dev_tensor(self):
        if self.__data is None:
            if not self.__info.data_read:
                raise TraceMismatchError("raw data of this tensor is not read in trace")
            self.__data = self.__info.data_reader.get_value()
        return self.__data

    def __del__(self):
        if self.__info.shape_read and self.__shape is not None:
            self.__info.shape_reader.drop_value()
        if self.__info.value_read and self.__value is not None:
            self.__info.value_reader.drop_value()
        if self.__info.data_read and self.__data is not None:
            self.__info.data_reader.drop_value()


class LazyEvalTensor(RawTensor):
    def __init__(self, varnode):
        self.__varnode = varnode

    @property
    def dtype(self):
        return self.__varnode.dtype

    @property
    def device(self):
        return self.__varnode.device

    @property
    def shape(self):
        return self.__varnode.shape

    def numpy(self):
        return self.__varnode.value

    def _dev_tensor(self):
        raise RuntimeError("cannot access data during symbolic tracing")


class TraceMixin:
    __subclass_cache = {}

    def __inject(self, handle):
        cache = __class__.__subclass_cache
        cls = self.__class__
        subcls = cache.get(cls)
        if subcls is None:
            subcls = cache[cls] = type("Traced" + cls.__name__, (__class__, cls), {})
        self.__class__ = subcls
        self.__handle = handle
        self.__cls = cls
        return self

    def __restore(self):
        cls = self.__cls
        del self.__handle
        del self.__cls
        self.__class__ = cls
        return self

    @property
    def shape(self):
        if not skip_tracing:
            active_trace._require_shape(self.__handle)
        return super().shape

    def numpy(self):
        if not skip_tracing:
            active_trace._require_value(self.__handle)
        return super().numpy()

    def _dev_tensor(self):
        if not skip_tracing:
            active_trace._require_data(self.__handle)
        return super()._dev_tensor()


class TracedRawTensor(TraceMixin, RawTensor):
    pass


class TracedLazyTensor(TraceMixin, LazyEvalTensor):
    pass


def assign_raw_tensor(lhs, rhs):
    handle = rhs._handle
    rhs.__dict__.clear()
    lhs.__dict__.clear()
    lhs.__class__ = RawTensor
    lhs.__init__(handle)


# this hook turns RawTensor into LazyEvalTensor
@apply.register()
def apply_symbolic_mode(op: OpDef, *args: RawTensor):
    graph = active_trace._lazy_eval_graph
    ivars = [
        getattr(x, "_LazyEvalTensor__varnode", None)
        or graph.make_const(x._dev_tensor())
        for x in args
    ]
    ovars = apply(op, *ivars)
    outputs = [LazyEvalTensor(v) for v in ovars]
    active_trace._lazy_eval_tensors.update(outputs)
    return outputs


apply.disable(apply_symbolic_mode)


@apply.register()
def apply_const_symbolic_mode(op: Const, *args: RawTensor):
    graph = active_trace._lazy_eval_graph
    ret = LazyEvalTensor(graph.make_const(op.value, dtype=op.dtype, device=op.device))
    active_trace._lazy_eval_tensors.add(ret)
    return (ret,)


apply.disable(apply_const_symbolic_mode)


@apply.register()
def apply_compiled_mode(op: OpDef, *args: RawTensor):
    if skip_tracing:
        args = [
            as_raw_tensor(x._dev_tensor()) if x.__class__ is CompiledTensorProxy else x
            for x in args
        ]
        return apply.super(op, *args)
    return active_trace._apply_op(op, args)


apply.disable(apply_compiled_mode)


# this hook injects TraceMixin
@apply.register()
def apply_with_tracing(op: OpDef, *args: RawTensor):
    outputs = apply.super(op, *args)
    active_trace._record_op(op, args, outputs)
    return outputs


apply.disable(apply_with_tracing)


@apply.register()
def apply_const_with_tracing(op: Const, *args: RawTensor):
    outputs = apply.super(op, *args)
    active_trace._record_const(op, outputs)
    return outputs


apply.disable(apply_const_with_tracing)


class BrokenRawTensor(RawTensor):
    def __getattribute__(self, _):
        raise RuntimeError("broken due to misuse of tracing")

    def __setattr__(self, *_):
        raise RuntimeError("broken due to misuse of tracing")


@functools.singledispatch
def find_raw_tensor(x):
    return None


@find_raw_tensor.register(RawTensor)
def _(x):
    return x


@find_raw_tensor.register(TensorWrapperBase)
def _(x):
    x = getattr(x, "__wrapped__", None)
    if x is not None:
        return find_raw_tensor(x)


@find_raw_tensor.register(Tensor)
def _(x):
    x = getattr(x, "_data", None)
    if x is not None:
        return find_raw_tensor(x)
