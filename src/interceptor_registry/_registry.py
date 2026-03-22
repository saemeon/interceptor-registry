import functools
import itertools
import logging
from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from types import MethodType
from typing import Any

_logger = logging.getLogger(__name__)

# Sentinel attribute stored on wrapper functions to identify them and recover
# their registry key when passed back to remove() / remove_all() / get_interceptors().
_REGISTRY_KEY_ATTR = "_interceptor_registry_key"


def _get_registry_key(method: MethodType) -> int:
    """Return the registry key for a method, unwrapping our own wrappers if needed.

    After `register` patches an object, subsequent lookups of the method return
    the wrapper. Storing the original key on the wrapper allows `remove` and
    friends to still find the right registry entry.
    """
    func = method.__func__
    if hasattr(func, _REGISTRY_KEY_ATTR):
        return getattr(func, _REGISTRY_KEY_ATTR)
    return id(func)


def _trigger_hook(obj, hook_func, pass_self, pass_args, pass_kwargs, *args, **kwargs) -> Any:
    """Execute a hook function with selectively forwarded arguments.

    Parameters
    ----------
    obj : Any
        The object owning the intercepted method.
    hook_func : callable
        The hook to execute.
    pass_self : bool
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
        The return value of `hook_func`.
    """
    _args: list[Any] = []
    _kwargs: dict[str, Any] = {}
    if pass_self:
        _args += [obj]
    if pass_args:
        _args += list(args)
    if pass_kwargs:
        _kwargs = kwargs
    return hook_func(*_args, **_kwargs)


def _call_if_is_callable(obj):
    """Call `obj` if it is callable and return its return value, otherwise return it unchanged."""
    if callable(obj):
        return obj()
    return obj


def _restore_original_method(obj, registry_key: int) -> None:
    """Remove the wrapped instance attribute, restoring class-level method lookup."""
    entry = obj._registered_interceptors_originals.pop(registry_key, None)
    if entry is not None:
        method_name, _ = entry
        if method_name in vars(obj):
            delattr(obj, method_name)
    del obj._registered_interceptors[registry_key]


def call_method_with_hooks(obj, method, registry_key: int, *args, **kwargs):
    """Call the method and trigger any interceptor registered for it.

    Interceptors are retrieved from `obj._registered_interceptors[registry_key]`
    and executed in ascending order of their resolved `callorder`.

    Ordering semantics
    ------------------
    - Interceptors with `callorder < 0` are executed before the method.
    - Interceptors with `callorder > 0` are executed after the method.
    - Smaller absolute values execute closer to the method call.

    `callorder` may be a numeric value or a callable. If callable, it is
    evaluated at invocation time to determine the effective order.

    Context manager interceptors
    ----------------------------
    Interceptors registered with `is_context_manager=True` are expected to
    return a context manager when called. The context is entered at the
    interceptor's position (pre or post) and managed via `contextlib.ExitStack`.
    All contexts are exited automatically in reverse order of entry (LIFO).

    Parameters
    ----------
    obj : Any
        The object owning the intercepted method and the registry.
    method : callable
        The original bound method to execute.
    registry_key : int
        Key under which interceptors are stored in `obj._registered_interceptors`.
    *args
        Positional arguments forwarded to the method and potentially to interceptors.
    **kwargs
        Keyword arguments forwarded to the method and potentially to interceptors.

    Returns
    -------
    Any
        The return value of the original method.
    """
    # Snapshot the hooks to avoid issues if the dict is mutated during execution.
    all_hooks = list(obj._registered_interceptors[registry_key].items())

    processed_hooks = []
    for iid, (func, pass_self, pass_args, pass_kwargs, is_cm, callorder) in all_hooks:
        resolved = _call_if_is_callable(callorder)
        if resolved == 0:
            raise ValueError(
                f"Interceptor {iid!r} for '{method.__name__}' resolved callorder=0, "
                "which is ambiguous. Use a negative value for pre-hooks "
                "or a positive value for post-hooks."
            )
        processed_hooks.append((func, pass_self, pass_args, pass_kwargs, is_cm, resolved))

    sorted_hooks = sorted(processed_hooks, key=lambda x: x[-1])

    with ExitStack() as stack:
        for hook_func, pass_self, pass_args, pass_kwargs, is_cm, order in sorted_hooks:
            if order < 0:
                rv = _trigger_hook(
                    obj, hook_func, pass_self, pass_args, pass_kwargs, *args, **kwargs
                )
                if is_cm:
                    stack.enter_context(rv)

        result = method(*args, **kwargs)

        for hook_func, pass_self, pass_args, pass_kwargs, is_cm, order in sorted_hooks:
            if order > 0:
                rv = _trigger_hook(
                    obj, hook_func, pass_self, pass_args, pass_kwargs, *args, **kwargs
                )
                if is_cm:
                    stack.enter_context(rv)

    return result


def register(
    method: MethodType,
    func: Callable,
    pass_self: bool = False,
    pass_args: bool = False,
    pass_kwargs: bool = False,
    is_context_manager: bool = False,
    callorder: int | float | Callable = 1,
) -> int:
    """Register an interceptor for a bound method.

    The interceptor is executed whenever the given method is called.
    Interceptors may run before the method, after the method, or wrap
    it in a context manager.

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
    is_context_manager : bool, optional
        If True, `func` is expected to return a context manager when
        called. The context is entered at the interceptor's position
        (before or after the method depending on `callorder`) and exited
        automatically via `contextlib.ExitStack`.
    callorder : int | float | Callable, optional, default 1
        Execution order of the interceptor.

        - Negative values execute before the method.
        - Positive values execute after the method.
        - Zero is invalid and raises `ValueError`.
        - Smaller absolute values execute closer to the method call.

        If callable, it is evaluated at each invocation to obtain the
        effective order. This allows dynamic ordering, e.g. passing
        `obj.get_zorder` for matplotlib artists.

    Returns
    -------
    int
        A unique registry identifier for use with `remove`.

    Raises
    ------
    ValueError
        If `callorder` is 0 (or a callable that resolves to 0 at call time).

    Examples
    --------
    >>> from contextlib import contextmanager
    >>> from interceptor_registry import register

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

    >>> register(foo.bar, print_before, callorder=-2)
    >>> register(foo.bar, around, is_context_manager=True, callorder=-1)

    >>> foo.bar()
    before
    enter context
    inside method call
    exit context

    'result of method'

    See Also
    --------
    [interceptor_registry.remove][]
    [interceptor_registry.remove_all][]
    [interceptor_registry.get_interceptors][]
    """
    if not callable(callorder) and callorder == 0:
        raise ValueError(
            "callorder=0 is invalid: use a negative value for pre-hooks "
            "or a positive value for post-hooks."
        )

    method_name = method.__name__
    obj: Any = method.__self__
    registry_key = _get_registry_key(method)

    if not hasattr(obj, "_registered_interceptors"):
        obj._registered_interceptors = defaultdict(dict)
        obj._registered_interceptors_id_gen = itertools.count()
        obj._registered_interceptors_originals = {}

    # Wrap method to trigger hooks on method call, if not already wrapped.
    if registry_key not in obj._registered_interceptors:
        obj._registered_interceptors_originals[registry_key] = (method_name, method)

        @functools.wraps(method)
        def wrapped(obj, *args, **kwargs):
            return call_method_with_hooks(obj, method, registry_key, *args, **kwargs)

        # Store the key on the wrapper so _get_registry_key can recover it later.
        setattr(wrapped, _REGISTRY_KEY_ATTR, registry_key)
        setattr(obj, method_name, MethodType(wrapped, obj))

    interceptor_id = next(obj._registered_interceptors_id_gen)
    obj._registered_interceptors[registry_key][interceptor_id] = (
        func, pass_self, pass_args, pass_kwargs, is_context_manager, callorder
    )

    _logger.debug(f"Register '{func}' to 'obj.{method_name}' on obj '{obj}'.")

    return interceptor_id


def remove(method: MethodType, interceptor_id: int) -> None:
    """Remove a previously registered interceptor from a bound method.

    When the last interceptor for a method is removed, the original method
    is restored and all registry state for that method is cleaned up.

    Parameters
    ----------
    method : MethodType
        The bound method from which to remove the interceptor.
    interceptor_id : int
        The registry identifier returned by `register`.

    Notes
    -----
    If the interceptor identifier is not found, this function silently
    returns without raising an error.

    Examples
    --------
    >>> from interceptor_registry import register, remove

    >>> class Foo:
    ...     def bar(self):
    ...         print("inside method call")
    ...         return "result of method"

    >>> foo = Foo()

    >>> def print_before():
    ...     print("before")

    >>> interceptor_id = register(foo.bar, print_before, callorder=-1)

    >>> foo.bar()
    before
    inside method call

    'result of method'
    >>> remove(foo.bar, interceptor_id)

    >>> foo.bar()
    inside method call

    'result of method'

    See Also
    --------
    [interceptor_registry.register][]
    [interceptor_registry.remove_all][]
    """
    obj: Any = method.__self__
    if not hasattr(obj, "_registered_interceptors"):
        return

    registry_key = _get_registry_key(method)

    if interceptor_id in obj._registered_interceptors[registry_key]:
        del obj._registered_interceptors[registry_key][interceptor_id]

    if not obj._registered_interceptors[registry_key]:
        _restore_original_method(obj, registry_key)


def remove_all(method: MethodType) -> None:
    """Remove all interceptors registered for a bound method and restore the original.

    Parameters
    ----------
    method : MethodType
        The bound method for which all interceptors should be removed.

    Notes
    -----
    If no interceptors are registered for the method, this function silently
    returns without raising an error.

    Examples
    --------
    >>> from interceptor_registry import register, remove_all

    >>> class Foo:
    ...     def bar(self):
    ...         return "result"

    >>> foo = Foo()
    >>> register(foo.bar, lambda: None, callorder=-1)
    >>> register(foo.bar, lambda: None, callorder=1)
    >>> remove_all(foo.bar)

    See Also
    --------
    [interceptor_registry.register][]
    [interceptor_registry.remove][]
    """
    obj: Any = method.__self__
    if not hasattr(obj, "_registered_interceptors"):
        return

    registry_key = _get_registry_key(method)
    if registry_key not in obj._registered_interceptors:
        return

    obj._registered_interceptors[registry_key].clear()
    _restore_original_method(obj, registry_key)


def get_interceptors(method: MethodType) -> list[dict[str, Any]]:
    """Return the list of registered interceptors for a bound method.

    Parameters
    ----------
    method : MethodType
        The bound method to introspect.

    Returns
    -------
    list[dict]
        One entry per registered interceptor, in registration order, each with:

        - ``id`` — the registry identifier (int)
        - ``func`` — the interceptor callable
        - ``pass_self`` — bool
        - ``pass_args`` — bool
        - ``pass_kwargs`` — bool
        - ``is_context_manager`` — bool
        - ``callorder`` — the raw registered value (may be callable)

        Returns an empty list if no interceptors are registered.

    Examples
    --------
    >>> from interceptor_registry import register, get_interceptors

    >>> class Foo:
    ...     def bar(self): pass

    >>> foo = Foo()
    >>> def hook(): pass
    >>> register(foo.bar, hook, callorder=-1)
    >>> get_interceptors(foo.bar)
    [{'id': 0, 'func': <function hook ...>, 'pass_self': False, 'pass_args': False,
      'pass_kwargs': False, 'is_context_manager': False, 'callorder': -1}]

    See Also
    --------
    [interceptor_registry.register][]
    """
    obj: Any = method.__self__
    if not hasattr(obj, "_registered_interceptors"):
        return []

    registry_key = _get_registry_key(method)
    if registry_key not in obj._registered_interceptors:
        return []

    return [
        {
            "id": interceptor_id,
            "func": func,
            "pass_self": pass_self,
            "pass_args": pass_args,
            "pass_kwargs": pass_kwargs,
            "is_context_manager": is_cm,
            "callorder": callorder,
        }
        for interceptor_id, (func, pass_self, pass_args, pass_kwargs, is_cm, callorder)
        in obj._registered_interceptors[registry_key].items()
    ]
