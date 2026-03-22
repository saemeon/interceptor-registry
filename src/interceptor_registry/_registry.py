import functools
import itertools
import logging
from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from types import MethodType

_logger = logging.getLogger(__name__)


def _trigger_hook(obj, hook_func, pass_obj, pass_args, pass_kwargs, *args, **kwargs) -> None:
    """Execute a hook function with selectively forwarded arguments.

    Parameters
    ----------
    obj : Any
        The object owning the intercepted method.
    hook_func : callable
        The hook to execute.
    pass_obj : bool
        If True, `obj` is passed as first positional argument.
    pass_args : bool
        If True, `*args` are forwarded.
    pass_kwargs : bool
        If True, `**kwargs` are forwarded.
    *args
        Positional arguments originally passed to the intercepted method.
    **kwargs
        Keyword arguments originally passed to the intercepted method.

    Returns
    -------
    Any
        The return value of `hook_func`. If `hook_func` is a generator function,
        the returned value is the generator instance.
    """
    _args = []
    _kwargs = {}
    if pass_obj:
        _args += [obj]
    if pass_args:
        _args += args
    if pass_kwargs:
        _kwargs = kwargs
    return hook_func(*_args, **_kwargs)


def _call_if_is_callable(obj):
    """Call `obj` if it is callable and return it's return value, otherwise return it unchanged."""
    if callable(obj):
        return obj()
    return obj


def _is_context_manager(obj):
    return hasattr(obj, "__enter__") and hasattr(obj, "__exit__")


def call_method_with_hooks(obj, method, registry_key, *args, **kwargs):
    """Call the method and trigger any interceptor registered for it.

    Interceptors are retrieved from `obj._registered_interceptors[registry_key]`
    and executed in ascending order of their resolved `callorder`.

    Ordering semantics
    ------------------
    - Interceptors with `callorder < 0` are executed before the method.
    - Interceptors with `callorder >= 0` are executed after the method.
    - Smaller `callorder` values execute earlier.

    `callorder` may be a numeric value or a callable. If callable, it is
    evaluated at invocation time to determine the effective order.

    Context manager interceptors
    ----------------------------
    An interceptor may behave as a context manager. In that case,
    `_trigger_hook(...)` must return an object implementing the context
    management protocol.

    - For pre-hooks (`callorder < 0`), the context is entered before the
      method call.
    - For post-hooks (`callorder >= 0`), the context is entered after the
      method call.
    - All entered contexts are managed via `contextlib.ExitStack`.
    - Contexts are exited automatically when leaving the `ExitStack`
      scope, in reverse order of entry (LIFO).

    Interceptors that are not context managers are executed directly and
    are not registered with the `ExitStack`.

    Parameters
    ----------
    obj : Any
        The object owning the intercepted method and the registry
        `obj._registered_interceptors`.
    method : callable
        The original bound method to execute.
    registry_key : str
        Key under which interceptors are stored in
        `obj._registered_interceptors`.
    *args
        Positional arguments forwarded to the method and potentially to
        interceptors.
    **kwargs
        Keyword arguments forwarded to the method and potentially to
        interceptors.

    Returns
    -------
    Any
        The return value of the original method.
    """
    all_hooks = obj._registered_interceptors[registry_key].values()

    # callorder may be a callable
    processed_hooks = [tuple(hook[:-1]) + (_call_if_is_callable(hook[-1]),) for hook in all_hooks]
    sorted_hooks = sorted(processed_hooks, key=lambda x: x[-1])

    with ExitStack() as stack:
        for hook_func, pass_obj, pass_args, pass_kwargs, order in sorted_hooks:
            if order < 0:
                maybe_cm = _trigger_hook(obj, hook_func, pass_obj, pass_args, pass_kwargs, *args, **kwargs)
                if _is_context_manager(maybe_cm):
                    stack.enter_context(maybe_cm)

        result = method(*args, **kwargs)

        for hook_func, pass_obj, pass_args, pass_kwargs, order in sorted_hooks:
            if order >= 0:
                maybe_cm = _trigger_hook(obj, hook_func, pass_obj, pass_args, pass_kwargs, *args, **kwargs)
                if _is_context_manager(maybe_cm):
                    stack.enter_context(maybe_cm)

    return result


def register_method_interceptor(
    method: MethodType,
    func: Callable,
    pass_self: bool = False,
    pass_args: bool = False,
    pass_kwargs: bool = False,
    callorder: int | float | Callable = 1,
) -> int:
    """
    Register an interceptor for a bound method.

    The interceptor is executed whenever the given method is called.
    Interceptors may run before the method, after the method, or
    open a context manager around the method.

    An interceptor may be:

    - A regular callable
    - A bound method
    - A callable returning a context manager

    If the interceptor returns a context manager, it is entered at
    execution time and managed via `contextlib.ExitStack`.

    - For `callorder < 0`, the context is entered before the method call.
    - For `callorder >= 0`, the context is entered after the method call.
    - Contexts are exited automatically when the surrounding call scope
      finishes.
    - Multiple context manager interceptors are exited in reverse order
      of entry (LIFO).

    Regular callables that do not return a context manager are simply
    executed at their designated position and do not participate in
    the managed scope.

    Parameters
    ----------
    method : MethodType
        A bound method (e.g. `obj.method`). The method must be bound,
        as the owning object is required for registration.
    func : Callable
        The interceptor to register.
    pass_self : bool, optional
        Whether to pass `self` (the object instance) of the `method`
        as first argument to the interceptor function.
    pass_args : bool, optional
        Whether to forward the method's positional arguments to the
        interceptor function.
    pass_kwargs : bool, optional
        Whether to forward the method's keyword arguments to the
        interceptor function.
    callorder : int | float | Callable, optional, default 1
        Execution order of the interceptor.

        - Negative values execute before the method.
        - Non-negative values execute after the method.
        - Smaller values execute earlier.

        If callable, it is evaluated at invocation time to obtain the
        effective order. This can be useful, for example, to patch
        matplotlib objects and pass `obj.get_zorder` as `callorder`.

    Returns
    -------
    int
        A unique registry identifier. This identifier can be used with
        `deregister_method_interceptor` to remove the interceptor.

    Examples
    --------
    >>> from contextlib import contextmanager
    >>> from interceptor_registry import register_method_interceptor

    >>> class Foo:
    ...     def bar(self):
    ...         print("inside method call")
    ...         return "result of method"

    >>> foo = Foo()

    >>> def print_before():
    ...     print("before")

    >>> @contextmanager
    ... def around():
    ...     print("enter context")
    ...     try:
    ...         yield
    ...     finally:
    ...         print("exit context")

    >>> register_method_interceptor(foo.bar, print_before, callorder=-2)
    >>> register_method_interceptor(foo.bar, around, callorder=-1)

    >>> foo.bar()
    before
    enter context
    inside method call
    exit context

    'result of method'

    See Also
    --------
    [interceptor_registry.deregister_method_interceptor][]
    """
    registry_key = method.__name__
    obj = method.__self__

    if not hasattr(obj, "_registered_interceptors"):
        obj._registered_interceptors = defaultdict(dict)
        obj._registered_interceptors_id_gen = itertools.count()

    # Wrap method to trigger hooks on method call, if not already wrapped
    if registry_key not in obj._registered_interceptors:

        @functools.wraps(method)
        def wrapped(obj, *args, **kwargs):
            return call_method_with_hooks(obj, method, registry_key, *args, **kwargs)

        setattr(obj, method.__name__, wrapped.__get__(obj, type(obj)))

    id = next(obj._registered_interceptors_id_gen)
    obj._registered_interceptors[registry_key][id] = (func, pass_self, pass_args, pass_kwargs, callorder)

    _logger.debug(f"Register '{func}' to  'obj.{method.__name__}' on obj '{obj}'.")

    return id


def deregister_method_interceptor(method, id) -> None:
    """Remove a previously registered interceptor from a bound method.

    Parameters
    ----------
    method : MethodType
        The bound method from which to remove the interceptor.
    id : int
        The registry identifier returned by `register_method_interceptor`.

    Notes
    -----
    If the interceptor identifier is not found, this function silently
    returns without raising an error.

    Examples
    --------
    >>> from interceptor_registry import register_method_interceptor, deregister_method_interceptor

    >>> class Foo:
    ...     def bar(self):
    ...         print("inside method call")
    ...         return "result of method"

    >>> foo = Foo()

    >>> def print_before():
    ...     print("before")

    >>> id = register_method_interceptor(foo.bar, print_before, callorder = -1)

    >>> foo.bar()
    before
    inside method call

    'result of method'
    >>> deregister_method_interceptor(foo.bar, id)

    >>> foo.bar()
    inside method call

    'result of method'

    See Also
    --------
    [interceptor_registry.register_method_interceptor][]
    """
    registry_key = method.__name__
    obj = method.__self__
    if not hasattr(obj, "_registered_interceptors"):
        return

    if id in obj._registered_interceptors[registry_key]:
        del obj._registered_interceptors[registry_key][id]


__all__ = ["register_method_interceptor", "deregister_method_interceptor"]
