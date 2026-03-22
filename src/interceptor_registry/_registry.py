import functools
import itertools
import logging
from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

_logger = logging.getLogger(__name__)

# Stamped on wrapper closures so the registry can be located from the
# already-patched instance attribute.
_REGISTRY_KEY_ATTR = "_interceptor_registry_key"
_INTERCEPTOR_OWNER_ATTR = "_interceptor_owner"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _lookup_raw_descriptor(cls: type, name: str):
    """Return the raw class-dict entry for *name*, walking the MRO."""
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    raise AttributeError(f"'{cls.__name__}' has no attribute '{name}'")


def _registry_key_for_descriptor(raw) -> int:
    """Return a stable ``id``-based key for a raw class-dict descriptor."""
    if isinstance(raw, (staticmethod, classmethod)):
        return id(raw.__func__)
    return id(raw)


def _get_registry_key(obj: Any, name: str) -> int | None:
    """Return the registry key for *name* on *obj* if it is currently patched."""
    existing = vars(obj).get(name)
    if existing is not None and hasattr(existing, _REGISTRY_KEY_ATTR):
        return getattr(existing, _REGISTRY_KEY_ATTR)
    return None


def _make_wrapper(obj: Any, original_callable, registry_key: int) -> Callable:
    """Return a plain-function closure that dispatches through the registry.

    Stored in ``obj.__dict__`` it shadows the class-level descriptor for all
    three kinds (instance method, classmethod, staticmethod) without triggering
    Python's descriptor re-binding on access.
    """
    source = getattr(original_callable, "__func__", original_callable)

    @functools.wraps(source)
    def wrapper(*args, **kwargs):
        return _call_method_with_hooks(
            obj, original_callable, registry_key, *args, **kwargs
        )

    setattr(wrapper, _REGISTRY_KEY_ATTR, registry_key)
    setattr(wrapper, _INTERCEPTOR_OWNER_ATTR, obj)
    return wrapper


def _trigger_hook(
    obj, hook_func, pass_self, pass_args, pass_kwargs, *args, **kwargs
) -> Any:
    """Call *hook_func*, forwarding owner / positional args / kwargs as requested."""
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
    """Return ``obj()`` if *obj* is callable, otherwise return *obj* unchanged."""
    if callable(obj):
        return obj()
    return obj


def _restore_original_method(obj, registry_key: int) -> None:
    """Delete the patched instance attribute, falling back to the class-level method."""
    entry = obj._registered_interceptors_originals.pop(registry_key, None)
    if entry is not None:
        method_name, _ = entry
        if method_name in vars(obj):
            delattr(obj, method_name)
    del obj._registered_interceptors[registry_key]


def _ensure_registry(obj: Any) -> None:
    """Initialise per-object registry attributes if not already present."""
    if not hasattr(obj, "_registered_interceptors"):
        obj._registered_interceptors = defaultdict(dict)
        obj._registered_interceptors_id_gen = itertools.count()
        obj._registered_interceptors_originals = {}


# ---------------------------------------------------------------------------
# Core execution engine
# ---------------------------------------------------------------------------


def _call_method_with_hooks(obj, method, registry_key: int, *args, **kwargs):
    """Execute *method* surrounded by all registered interceptors.

    Pre-hooks (``callorder < 0``) run before the method in ascending order;
    post-hooks (``callorder > 0``) run after in ascending order.  Interceptors
    with ``is_context_manager=True`` must return a context manager; its
    ``__enter__`` is called at the hook's position and ``__exit__`` is called
    automatically via ``contextlib.ExitStack`` when the outermost scope exits
    (LIFO across all registered contexts).

    ``callorder`` may be a callable — evaluated fresh on every call.
    ``callorder=0`` raises ``ValueError``.
    """
    # Snapshot so mutations during execution don't affect the current call.
    all_hooks = list(obj._registered_interceptors[registry_key].items())

    processed_hooks = []
    for iid, (func, pass_self, pass_args, pass_kwargs, is_cm, callorder) in all_hooks:
        resolved = _call_if_is_callable(callorder)
        if resolved == 0:
            raise ValueError(
                f"Interceptor {iid!r} for '{getattr(method, '__name__', method)}' "
                "resolved callorder=0. Use a negative value for pre-hooks "
                "or a positive value for post-hooks."
            )
        processed_hooks.append(
            (func, pass_self, pass_args, pass_kwargs, is_cm, resolved)
        )

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_interceptor(
    obj,
    name: str,
    func: Callable,
    pass_self: bool = False,
    pass_args: bool = False,
    pass_kwargs: bool = False,
    is_context_manager: bool = False,
    callorder: int | float | Callable = 1,
) -> int:
    """Add an interceptor on a callable attribute of *obj*.

    Works for instance methods, classmethods, and static methods.
    Interception is always scoped to *obj* — other instances are not affected.
    Multiple interceptors stack; each call adds one more.

    Parameters
    ----------
    obj : Any
        Object on which to add the interceptor.
    name : str
        Attribute name of the callable to intercept (e.g. ``'draw'``).
    func : Callable
        The interceptor.
    pass_self : bool, optional
        Pass *obj* as the first argument to *func*.
    pass_args : bool, optional
        Forward the intercepted call's positional arguments to *func*.
    pass_kwargs : bool, optional
        Forward the intercepted call's keyword arguments to *func*.
    is_context_manager : bool, optional
        Treat *func*'s return value as a context manager.  Its ``__enter__``
        is called at the interceptor's position; ``__exit__`` is called
        automatically when the call scope exits.
    callorder : int | float | Callable, optional, default 1
        Execution order.  Negative = before the method, positive = after.
        Sorted ascending: ``-2`` runs before ``-1``, ``1`` before ``2``.
        Zero is invalid.  If callable, evaluated on every invocation.

    Returns
    -------
    int
        Unique registry identifier — pass to ``del_interceptor`` to remove.

    Raises
    ------
    ValueError
        If ``callorder`` is ``0``, or resolves to ``0`` at call time.

    Notes
    -----
    The interceptor is patched directly on *obj*'s instance ``__dict__``, so
    it shadows the class-level descriptor only for that specific instance.
    The original callable is restored automatically once all interceptors for
    that attribute have been removed.

    Examples
    --------
    Pre- and post-hooks:

    >>> from interceptor_registry import add_interceptor

    >>> class Foo:
    ...     def bar(self, x): return x * 2

    >>> foo = Foo()
    >>> add_interceptor(foo, 'bar', lambda: print("before"), callorder=-1)
    0
    >>> add_interceptor(foo, 'bar', lambda: print("after"), callorder=1)
    1
    >>> foo.bar(3)
    before
    after
    6

    Context manager around the call:

    >>> from contextlib import contextmanager
    >>> from interceptor_registry import add_interceptor

    >>> @contextmanager
    ... def timing():
    ...     print("start")
    ...     yield
    ...     print("end")

    >>> foo2 = Foo()
    >>> add_interceptor(foo2, 'bar', timing, is_context_manager=True, callorder=-1)
    0
    >>> foo2.bar(3)
    start
    end
    6
    """
    if not callable(callorder) and callorder == 0:
        raise ValueError(
            "callorder=0 is invalid: use a negative value for pre-hooks "
            "or a positive value for post-hooks."
        )

    obj_any: Any = obj

    # Already patched — recover the key and append without re-patching.
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is not None:
        _ensure_registry(obj_any)
        interceptor_id = next(obj_any._registered_interceptors_id_gen)
        obj_any._registered_interceptors[registry_key][interceptor_id] = (
            func, pass_self, pass_args, pass_kwargs, is_context_manager, callorder
        )
        _logger.debug(f"Add interceptor '{func}' on 'obj.{name}' (obj={obj!r}).")
        return interceptor_id

    raw = _lookup_raw_descriptor(type(obj_any), name)
    registry_key = _registry_key_for_descriptor(raw)

    _ensure_registry(obj_any)

    if registry_key not in obj_any._registered_interceptors:
        original_callable = getattr(obj_any, name)
        obj_any._registered_interceptors_originals[registry_key] = (
            name, original_callable
        )
        wrapper = _make_wrapper(obj_any, original_callable, registry_key)
        setattr(obj_any, name, wrapper)

    interceptor_id = next(obj_any._registered_interceptors_id_gen)
    obj_any._registered_interceptors[registry_key][interceptor_id] = (
        func, pass_self, pass_args, pass_kwargs, is_context_manager, callorder
    )
    _logger.debug(f"Add interceptor '{func}' on 'obj.{name}' (obj={obj!r}).")
    return interceptor_id


def del_interceptor(obj, name: str, interceptor_id: int) -> None:
    """Remove one interceptor by id.

    Restores the original callable when the last interceptor is removed.
    Silently does nothing if *interceptor_id* is not found or *name* is
    not currently patched on *obj*.

    Parameters
    ----------
    obj : Any
        The same object passed to ``add_interceptor``.
    name : str
        The same attribute name passed to ``add_interceptor``.
    interceptor_id : int
        Value returned by ``add_interceptor``.

    Notes
    -----
    When the last interceptor for *name* is removed the original callable is
    restored and all registry state for that attribute is cleaned up.

    Examples
    --------
    >>> from interceptor_registry import add_interceptor, del_interceptor

    >>> class Foo:
    ...     def bar(self): return "result"

    >>> foo = Foo()
    >>> iid = add_interceptor(foo, 'bar', lambda: print("before"), callorder=-1)
    >>> foo.bar()
    before
    'result'
    >>> del_interceptor(foo, 'bar', iid)
    >>> foo.bar()
    'result'
    """
    obj_any: Any = obj
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None or not hasattr(obj_any, "_registered_interceptors"):
        return

    if interceptor_id in obj_any._registered_interceptors[registry_key]:
        del obj_any._registered_interceptors[registry_key][interceptor_id]

    if not obj_any._registered_interceptors[registry_key]:
        _restore_original_method(obj_any, registry_key)


def del_interceptors(obj, name: str) -> None:
    """Remove all interceptors for *name* on *obj* and restore the original.

    Silently does nothing if *name* is not currently patched on *obj*.

    Parameters
    ----------
    obj : Any
        The same object passed to ``add_interceptor``.
    name : str
        The same attribute name passed to ``add_interceptor``.

    Examples
    --------
    >>> from interceptor_registry import add_interceptor, del_interceptors

    >>> class Foo:
    ...     def bar(self): return "result"

    >>> foo = Foo()
    >>> add_interceptor(foo, 'bar', lambda: print("a"), callorder=-2)
    0
    >>> add_interceptor(foo, 'bar', lambda: print("b"), callorder=-1)
    1
    >>> del_interceptors(foo, 'bar')
    >>> foo.bar()
    'result'
    """
    obj_any: Any = obj
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None or not hasattr(obj_any, "_registered_interceptors"):
        return
    if registry_key not in obj_any._registered_interceptors:
        return

    obj_any._registered_interceptors[registry_key].clear()
    _restore_original_method(obj_any, registry_key)


def has_interceptors(obj, name: str) -> bool:
    """Return ``True`` if *name* on *obj* has any registered interceptors.

    Parameters
    ----------
    obj : Any
        The same object passed to ``add_interceptor``.
    name : str
        The same attribute name passed to ``add_interceptor``.

    Examples
    --------
    >>> from interceptor_registry import (
    ...     add_interceptor, del_interceptors, has_interceptors
    ... )

    >>> class Foo:
    ...     def bar(self): pass

    >>> foo = Foo()
    >>> has_interceptors(foo, 'bar')
    False
    >>> add_interceptor(foo, 'bar', lambda: None, callorder=-1)
    0
    >>> has_interceptors(foo, 'bar')
    True
    >>> del_interceptors(foo, 'bar')
    >>> has_interceptors(foo, 'bar')
    False
    """
    obj_any: Any = obj
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None or not hasattr(obj_any, "_registered_interceptors"):
        return False
    return bool(obj_any._registered_interceptors.get(registry_key))


def get_interceptors(obj, name: str) -> list[dict[str, Any]]:
    """Return all interceptors added for *name* on *obj*, in registration order.

    Parameters
    ----------
    obj : Any
        The same object passed to ``add_interceptor``.
    name : str
        The same attribute name passed to ``add_interceptor``.

    Returns
    -------
    list[dict]
        Each dict contains ``id``, ``func``, ``pass_self``, ``pass_args``,
        ``pass_kwargs``, ``is_context_manager``, and ``callorder`` (raw
        registered value — may be a callable).  Empty list if none registered.
    """
    obj_any: Any = obj
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None or not hasattr(obj_any, "_registered_interceptors"):
        return []
    if registry_key not in obj_any._registered_interceptors:
        return []

    return [
        {
            "id": iid,
            "func": func,
            "pass_self": pass_self,
            "pass_args": pass_args,
            "pass_kwargs": pass_kwargs,
            "is_context_manager": is_cm,
            "callorder": callorder,
        }
        for iid, (func, pass_self, pass_args, pass_kwargs, is_cm, callorder)
        in obj_any._registered_interceptors[registry_key].items()
    ]
