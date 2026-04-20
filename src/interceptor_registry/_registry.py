import asyncio
import contextlib
import functools
import inspect
import itertools
import logging
import threading
import weakref
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

# Stamped on wrapper closures so the registry can be located from the
# already-patched instance attribute.
_REGISTRY_KEY_ATTR = "_interceptor_registry_key"
_INTERCEPTOR_OWNER_ATTR = "_interceptor_owner"

# Attribute name used as a fallback store for non-weakrefable targets.
_REGISTRY_FALLBACK_ATTR = "__interceptor_registry__"


# ---------------------------------------------------------------------------
# Per-target registry state
# ---------------------------------------------------------------------------


@dataclass
class _Registry:
    """Per-target registry state.

    Holds every interceptor, the id generator, the originals map, and the
    per-object reentrant lock.  One instance per intercepted target object
    — kept in a module-level ``WeakKeyDictionary`` so it dies with the
    target.

    Attributes
    ----------
    interceptors : dict[int, dict[int, tuple]]
        Mapping ``registry_key -> {interceptor_id: (func, pass_self,
        pass_args, pass_kwargs, is_cm, callorder)}``.
    id_gen : Iterator[int]
        Monotonic counter used to mint unique interceptor ids.
    originals : dict[int, str]
        Mapping ``registry_key -> attribute_name`` used to restore the
        pre-patch state when the last interceptor is removed.  Only the
        name is stored — the original callable is captured by the
        wrapper closure, which is self-referential with *obj* and
        therefore doesn't prevent garbage collection.
    lock : threading.RLock
        Per-target reentrant lock.  Held by the three mutating public
        entry points (``add_interceptor``, ``del_interceptor``,
        ``del_interceptors``); read-only introspection is lock-free.
    """

    interceptors: dict[int, dict[int, tuple]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    id_gen: Iterator[int] = field(default_factory=itertools.count)
    originals: dict[int, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


_REGISTRIES: "weakref.WeakKeyDictionary[Any, _Registry]" = weakref.WeakKeyDictionary()
_REGISTRIES_LOCK = threading.Lock()


def _get_registry(obj: Any, create: bool = True) -> _Registry | None:
    """Return the :class:`_Registry` attached to *obj*, creating it on demand.

    Parameters
    ----------
    obj : Any
        The target object whose registry is needed.
    create : bool, optional
        If True (default), create and store a fresh registry when none
        exists.  If False, return ``None`` instead.

    Returns
    -------
    _Registry or None
        The per-target registry, or ``None`` if *create* is False and no
        registry exists.

    Notes
    -----
    For ordinary objects the registry lives in a module-level
    :class:`weakref.WeakKeyDictionary`, so the registry is garbage-
    collected with the target.  Objects that cannot be weak-referenced
    (rare — typically those with ``__slots__`` that exclude
    ``__weakref__``) fall back to a private ``__interceptor_registry__``
    instance attribute.
    """
    try:
        existing = _REGISTRIES.get(obj)
    except TypeError:
        # Object is not weakrefable — fall back to attribute storage.
        reg = getattr(obj, _REGISTRY_FALLBACK_ATTR, None)
        if reg is None:
            if not create:
                return None
            reg = _Registry()
            try:
                setattr(obj, _REGISTRY_FALLBACK_ATTR, reg)
            except (AttributeError, TypeError) as exc:
                raise TypeError(
                    f"Cannot store interceptor registry on "
                    f"{type(obj).__name__}: object is not weak-referenceable "
                    "and does not allow attribute assignment."
                ) from exc
        return reg

    if existing is not None:
        return existing
    if not create:
        return None

    with _REGISTRIES_LOCK:
        # setdefault so concurrent creations race cleanly.
        return _REGISTRIES.setdefault(obj, _Registry())


def _drop_registry(obj: Any) -> None:
    """Delete the :class:`_Registry` for *obj* if one exists.

    Called once the last interceptor for *obj* has been removed so state
    doesn't linger on long-lived targets.  Silent no-op if no registry
    exists.
    """
    try:
        _REGISTRIES.pop(obj, None)
    except TypeError:
        # Non-weakrefable fallback path.
        if hasattr(obj, _REGISTRY_FALLBACK_ATTR):
            with contextlib.suppress(AttributeError):
                delattr(obj, _REGISTRY_FALLBACK_ATTR)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _lookup_raw_descriptor(cls: type, name: str):
    """Return the raw class-dict entry for *name*, walking the MRO.

    Parameters
    ----------
    cls : type
        The class to search.
    name : str
        Attribute name to look up.

    Returns
    -------
    Any
        The raw value stored in a class ``__dict__`` (e.g. function,
        classmethod, staticmethod, property, plain attribute).

    Raises
    ------
    AttributeError
        If *name* is not found in any class along the MRO.
    """
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    raise AttributeError(f"'{cls.__name__}' has no attribute '{name}'")


def _registry_key_for_descriptor(raw) -> int:
    """Return a stable ``id``-based key for a raw class-dict descriptor."""
    if isinstance(raw, staticmethod | classmethod):
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

    The returned wrapper mirrors the callable kind of *original_callable*
    (sync, async, generator, or async-generator) so that ``await`` /
    iteration semantics on the caller side are preserved and pre/post
    hooks wrap the actual body, not the builder-call that returns a
    coroutine / generator.

    Parameters
    ----------
    obj : Any
        The target object whose method is being wrapped.
    original_callable : Callable
        The pre-patch bound callable (or function) the wrapper defers to.
    registry_key : int
        The registry key used to look up hooks at call time.

    Returns
    -------
    Callable
        A function wrapper stored in ``obj.__dict__[name]`` that shadows
        the class-level descriptor for this one instance.
    """
    source = getattr(original_callable, "__func__", original_callable)

    if asyncio.iscoroutinefunction(source):

        @functools.wraps(source)
        async def wrapper(*args, **kwargs):
            return await _call_method_with_hooks_async(
                obj, original_callable, registry_key, *args, **kwargs
            )
    elif inspect.isasyncgenfunction(source):
        # The wrapper must itself be an async generator (it contains
        # ``yield``).  We forward ``asend`` / ``athrow`` / ``aclose``
        # directly here rather than inside a helper so that the
        # wrapper's own ``yield`` expression is the forwarding point
        # — interposing an ``async for v in helper(): yield v`` would
        # swallow all three forwarding operations at the wrapper level
        # even if the helper implemented them correctly internally.
        @functools.wraps(source)
        async def wrapper(*args, **kwargs):
            sorted_hooks = _prepare_hooks(obj, original_callable, registry_key)
            agen = original_callable(*args, **kwargs)
            with ExitStack() as stack:
                _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
                try:
                    try:
                        value = await agen.__anext__()
                    except StopAsyncIteration:
                        # Empty async generator.
                        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)
                        return
                    while True:
                        try:
                            sent = yield value
                        except GeneratorExit:  # noqa: PERF203
                            # aclose() on the wrapper — forward to body.
                            # The try/except wrapping the yield is the
                            # whole point of the forwarding pattern; it
                            # cannot be lifted out of the loop.
                            await agen.aclose()
                            raise
                        except BaseException as exc:
                            # athrow() on the wrapper — forward into the
                            # body's ``yield`` expression.  If the body
                            # catches and yields another value, use it.
                            # If the body re-raises, athrow propagates
                            # here and is re-raised naturally.
                            try:
                                value = await agen.athrow(exc)
                            except StopAsyncIteration:
                                break
                        else:
                            try:
                                if sent is None:
                                    value = await agen.__anext__()
                                else:
                                    value = await agen.asend(sent)
                            except StopAsyncIteration:
                                break
                finally:
                    # Always finalise the underlying agen — idempotent.
                    await agen.aclose()
                _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)
    elif inspect.isgeneratorfunction(source):
        # The wrapper itself is a generator (contains ``yield from``).
        # ``yield from`` forwards ``send`` / ``throw`` / ``close`` into
        # the helper, which in turn forwards them into the body.  The
        # ``= yield from`` capture preserves the body's PEP-380 return
        # value so the caller sees it via ``StopIteration.value``.
        @functools.wraps(source)
        def wrapper(*args, **kwargs):
            result = yield from _call_method_with_hooks_gen(
                obj, original_callable, registry_key, *args, **kwargs
            )
            return result
    else:

        @functools.wraps(source)
        def wrapper(*args, **kwargs):
            return _call_method_with_hooks(
                obj, original_callable, registry_key, *args, **kwargs
            )

    setattr(wrapper, _REGISTRY_KEY_ATTR, registry_key)
    setattr(wrapper, _INTERCEPTOR_OWNER_ATTR, obj)
    return wrapper


def _trigger_hook(
    obj, hook_func, pass_self, pass_args, pass_kwargs, is_cm, *args, **kwargs
) -> Any:
    """Call *hook_func*, forwarding owner / positional args / kwargs as requested.

    Parameters
    ----------
    obj : Any
        The target instance; forwarded as the first argument when
        ``pass_self`` is True.
    hook_func : Callable
        The user-supplied interceptor callable.
    pass_self, pass_args, pass_kwargs : bool
        Forwarding switches (see :func:`add_interceptor`).
    is_cm : bool
        Whether the hook is marked as a context manager.  Only used to
        shape the error message when an async CM slips through.
    *args, **kwargs
        Positional / keyword arguments captured from the intercepted call.

    Returns
    -------
    Any
        Whatever ``hook_func`` returns.  For ``is_cm=True`` this must be
        a synchronous context manager.

    Raises
    ------
    TypeError
        If ``hook_func`` is an ``async def`` (coroutine function) or an
        async generator.  Async hooks are not supported in v0.2.
    """
    if asyncio.iscoroutinefunction(hook_func) or inspect.isasyncgenfunction(hook_func):
        raise TypeError(
            f"Async hooks are not supported: {hook_func!r} is a coroutine or "
            "async-generator function.  Wrap your async logic in a synchronous "
            "hook, or file a feature request for async-hook support."
        )
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
    """Delete the patched instance attribute, falling back to the class-level method.

    Also deletes the per-key interceptor bucket and, when no buckets remain,
    drops the whole :class:`_Registry` via :func:`_drop_registry`.
    """
    registry = _get_registry(obj, create=False)
    if registry is None:
        return

    method_name = registry.originals.pop(registry_key, None)
    if method_name is not None and method_name in vars(obj):
        delattr(obj, method_name)
    registry.interceptors.pop(registry_key, None)

    if not registry.interceptors and not registry.originals:
        _drop_registry(obj)


# ---------------------------------------------------------------------------
# Core execution engine
# ---------------------------------------------------------------------------


def _prepare_hooks(obj, method, registry_key: int) -> list[tuple]:
    """Snapshot, resolve callable callorders, validate, and sort hooks.

    Parameters
    ----------
    obj : Any
        The target object whose registry is consulted.
    method : Callable
        The original callable — used only for error-message text.
    registry_key : int
        The registry bucket being dispatched.

    Returns
    -------
    list of tuple
        ``(func, pass_self, pass_args, pass_kwargs, is_cm, resolved_order)``
        sorted ascending by ``resolved_order``.

    Raises
    ------
    ValueError
        If any hook's callorder resolves to ``0`` at call time.
    """
    registry = _get_registry(obj, create=False)
    if registry is None:
        return []

    # Snapshot so mutations during execution don't affect the current call.
    all_hooks = list(registry.interceptors[registry_key].items())

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

    return sorted(processed_hooks, key=lambda x: x[-1])


def _run_pre_hooks(
    obj, sorted_hooks: list[tuple], stack: ExitStack, args: tuple, kwargs: dict
) -> None:
    """Dispatch every hook with ``resolved_order < 0`` in ascending order.

    ``is_cm=True`` hooks have their returned value entered on *stack* so
    their ``__exit__`` fires (in LIFO order) when the stack closes —
    after the intercepted body *and* all post hooks have run.

    Raises
    ------
    TypeError
        If a CM hook returns an async context manager, or if a hook is
        an async callable.
    """
    for hook_func, pass_self, pass_args, pass_kwargs, is_cm, order in sorted_hooks:
        if order < 0:
            rv = _trigger_hook(
                obj,
                hook_func,
                pass_self,
                pass_args,
                pass_kwargs,
                is_cm,
                *args,
                **kwargs,
            )
            if is_cm:
                if not hasattr(rv, "__enter__"):
                    raise TypeError(
                        f"Interceptor {hook_func!r} returned "
                        f"{type(rv).__name__}, which is not a synchronous "
                        "context manager.  Async context managers are not "
                        "supported in this version."
                    )
                stack.enter_context(rv)


def _run_post_hooks(
    obj, sorted_hooks: list[tuple], stack: ExitStack, args: tuple, kwargs: dict
) -> None:
    """Dispatch every hook with ``resolved_order > 0`` in ascending order.

    Behaves symmetrically to :func:`_run_pre_hooks` but for positive
    orders.
    """
    for hook_func, pass_self, pass_args, pass_kwargs, is_cm, order in sorted_hooks:
        if order > 0:
            rv = _trigger_hook(
                obj,
                hook_func,
                pass_self,
                pass_args,
                pass_kwargs,
                is_cm,
                *args,
                **kwargs,
            )
            if is_cm:
                if not hasattr(rv, "__enter__"):
                    raise TypeError(
                        f"Interceptor {hook_func!r} returned "
                        f"{type(rv).__name__}, which is not a synchronous "
                        "context manager.  Async context managers are not "
                        "supported in this version."
                    )
                stack.enter_context(rv)


def _call_method_with_hooks(obj, method, registry_key: int, *args, **kwargs):
    """Execute a synchronous *method* surrounded by all registered interceptors.

    Pre-hooks (``callorder < 0``) run before the method in ascending order;
    post-hooks (``callorder > 0``) run after in ascending order.  Hooks
    with ``is_context_manager=True`` must return a synchronous context
    manager; its ``__enter__`` is called at the hook's position and
    ``__exit__`` is called automatically via :class:`contextlib.ExitStack`
    when the outermost scope exits (LIFO across all entered contexts).

    If a pre-hook CM's ``__exit__`` returns ``True`` to suppress an
    exception raised by the method body, the dispatcher returns
    ``None`` — the method's nominal result never existed, and the
    suppression is honoured.  Post-hooks are skipped in this case
    because the body exception is handled only once the CM stack
    unwinds, which is after the dispatcher has already left
    ``_run_post_hooks``.

    Raises
    ------
    ValueError
        If any hook's callorder resolves to ``0``.
    TypeError
        If a CM hook returns a non-synchronous context manager, or a hook
        is an async callable.
    """
    sorted_hooks = _prepare_hooks(obj, method, registry_key)

    # Initialise ``result`` before entering the ExitStack so that a
    # CM-hook whose ``__exit__`` suppresses an exception raised by the
    # method body doesn't leave this name unbound for the final
    # ``return``.  Suppression therefore yields ``None``.
    result = None
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        result = method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)

    return result


async def _call_method_with_hooks_async(
    obj, method, registry_key: int, *args, **kwargs
):
    """Async analogue of :func:`_call_method_with_hooks`.

    Awaits ``method(*args, **kwargs)`` inside the :class:`ExitStack` so
    CM hooks wrap the awaited body, not the coroutine construction.
    Pre/post hooks themselves stay synchronous — async hooks are out of
    scope for this version and raise :class:`TypeError` at dispatch
    time.

    Suppression semantics match the sync dispatcher: if a CM hook
    swallows the awaited body's exception the dispatcher returns
    ``None``.
    """
    sorted_hooks = _prepare_hooks(obj, method, registry_key)

    # See :func:`_call_method_with_hooks` for why ``result`` is
    # initialised eagerly.
    result = None
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        result = await method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)

    return result


def _call_method_with_hooks_gen(obj, method, registry_key: int, *args, **kwargs):
    """Generator analogue of :func:`_call_method_with_hooks`.

    Yields from ``method(*args, **kwargs)`` inside the stack so the CM
    exits only after full iteration (or early close, because
    :class:`ExitStack` cleans up on GeneratorExit).

    Captures the ``StopIteration.value`` of the wrapped generator (the
    ``return <value>`` expression, per PEP 380) and re-surfaces it as
    the wrapper's own stop value so callers using ``result = yield from
    obj.m()`` observe the underlying return value unchanged.

    If a pre-hook CM swallows an exception that reached the generator
    (either raised by the body or injected via ``gen.throw``), the
    wrapper returns ``None`` instead of propagating — mirroring the
    sync dispatcher's suppression semantics.
    """
    sorted_hooks = _prepare_hooks(obj, method, registry_key)

    # See :func:`_call_method_with_hooks` for the ``result = None``
    # rationale.  Generators additionally use ``result`` as the
    # ``StopIteration.value`` the wrapper itself surfaces — capturing
    # the PEP 380 return value of the wrapped body.
    result = None
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        result = yield from method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)

    return result


# Note: there is intentionally no ``_call_method_with_hooks_async_gen``
# helper.  Forwarding ``asend`` / ``athrow`` / ``aclose`` through a
# separate helper would require the outer wrapper to iterate the helper
# with ``async for ...: yield v``, which breaks all three operations at
# the wrapper level.  The async-gen logic therefore lives inline inside
# :func:`_make_wrapper` so that the wrapper's own ``yield`` is the
# forwarding point.


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

    Works for instance methods, classmethods, staticmethods, ``async def``
    methods, generator methods, and async-generator methods.  Interception
    is always scoped to *obj* — other instances of the same class are not
    affected.  Multiple interceptors stack; each call adds one more.

    Parameters
    ----------
    obj : Any
        Object on which to add the interceptor.
    name : str
        Attribute name of the callable to intercept (e.g. ``'draw'``).
    func : Callable
        The interceptor. Must be synchronous — ``async def`` / async-generator
        hooks are rejected with :class:`TypeError` at registration.
    pass_self : bool, optional
        Pass *obj* as the first argument to *func*. By default False.
    pass_args : bool, optional
        Forward the intercepted call's positional arguments to *func*.
        By default False.
    pass_kwargs : bool, optional
        Forward the intercepted call's keyword arguments to *func*.
        By default False.
    is_context_manager : bool, optional
        Treat *func*'s return value as a synchronous context manager.
        Its ``__enter__`` is called at the interceptor's position;
        ``__exit__`` is called automatically when the call scope exits,
        receiving any exception raised by the method body or by
        later hooks. Async context managers are not supported. By
        default False.
    callorder : int | float | Callable, optional
        Execution order. Negative = before the method, positive =
        after. Sorted ascending: ``-2`` runs before ``-1``, ``1``
        before ``2``. Zero is invalid. If callable, evaluated on
        every invocation. By default 1.

    Returns
    -------
    int
        Unique registry identifier — pass to :func:`del_interceptor` to
        remove.

    Raises
    ------
    ValueError
        If ``callorder`` is ``0``, or resolves to ``0`` at call time.
    TypeError
        If *name* resolves to a :class:`property` or another non-callable
        descriptor (supported kinds are instance method, classmethod,
        staticmethod, and async / generator variants thereof).  Also
        raised at registration time if *func* is an ``async def`` or
        async generator function (async hooks are out of scope).

    See Also
    --------
    del_interceptor : Remove one interceptor by its registry id.
    del_interceptors : Remove every interceptor for a given name.
    has_interceptors : Check whether any interceptor is registered.
    get_interceptors : Snapshot every interceptor registered on *name*.

    Notes
    -----
    The interceptor is patched directly on *obj*'s instance
    ``__dict__``, so it shadows the class-level descriptor only for
    that specific instance. The original callable is restored
    automatically once all interceptors for that attribute have been
    removed.

    Registry state is held in a module-level
    :class:`weakref.WeakKeyDictionary` keyed by *obj* — nothing is
    written to *obj*'s instance dict beyond the wrapper itself.
    Targets with ``__slots__`` that exclude ``__weakref__`` fall
    back to an ``__interceptor_registry__`` attribute on the
    instance.

    When a context-manager hook's ``__exit__`` suppresses an
    exception raised by the method body (returns ``True``), the
    wrapped call returns ``None`` for non-generator methods;
    generator and async-generator methods terminate without
    further yields.

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

    if asyncio.iscoroutinefunction(func) or inspect.isasyncgenfunction(func):
        raise TypeError(
            f"Async hooks are not supported in v0.2: {func!r} is a coroutine "
            "or async-generator function.  Wrap your async logic in a "
            "synchronous hook, or file a feature request for async-hook "
            "support."
        )

    obj_any: Any = obj

    # Already patched — recover the key and append without re-patching.
    # The lock is acquired via the registry we already hold.
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is not None:
        registry = _get_registry(obj_any, create=True)
        assert registry is not None
        with registry.lock:
            interceptor_id = next(registry.id_gen)
            registry.interceptors[registry_key][interceptor_id] = (
                func,
                pass_self,
                pass_args,
                pass_kwargs,
                is_context_manager,
                callorder,
            )
        _logger.debug(f"Add interceptor '{func}' on 'obj.{name}' (obj={obj!r}).")
        return interceptor_id

    raw = _lookup_raw_descriptor(type(obj_any), name)

    if isinstance(raw, property):
        raise TypeError(
            f"Cannot intercept property '{name}' on {type(obj_any).__name__}: "
            "interceptor-registry supports callable descriptors only "
            "(instance methods, classmethods, staticmethods, and their "
            "async / generator variants).  To intercept a property's "
            "getter or setter, wrap the underlying function and rebuild "
            "the property on the class."
        )
    if not callable(raw) and not isinstance(raw, staticmethod | classmethod):
        raise TypeError(
            f"Cannot intercept '{name}' on {type(obj_any).__name__}: "
            f"{type(raw).__name__} is not a supported descriptor kind.  "
            "Supported: instance method, classmethod, staticmethod, "
            "async method, generator method, async-generator method."
        )

    registry_key = _registry_key_for_descriptor(raw)
    registry = _get_registry(obj_any, create=True)
    assert registry is not None

    with registry.lock:
        # Re-check inside the lock — another thread may have raced us
        # past the outer ``_get_registry_key`` probe.
        existing_key = _get_registry_key(obj_any, name)
        if existing_key is not None:
            # Already patched by a concurrent caller: just append.
            registry_key = existing_key
        elif registry_key not in registry.interceptors:
            original_callable = getattr(obj_any, name)
            registry.originals[registry_key] = name
            wrapper = _make_wrapper(obj_any, original_callable, registry_key)
            setattr(obj_any, name, wrapper)

        interceptor_id = next(registry.id_gen)
        registry.interceptors[registry_key][interceptor_id] = (
            func,
            pass_self,
            pass_args,
            pass_kwargs,
            is_context_manager,
            callorder,
        )

    _logger.debug(f"Add interceptor '{func}' on 'obj.{name}' (obj={obj!r}).")
    return interceptor_id


def del_interceptor(obj, name: str, interceptor_id: int) -> None:
    """Remove one interceptor by id.

    Restores the original callable when the last interceptor for *name*
    is removed.  Silently does nothing if *interceptor_id* is not found
    or *name* is not currently patched on *obj*.

    Parameters
    ----------
    obj : Any
        The same object passed to :func:`add_interceptor`.
    name : str
        The same attribute name passed to :func:`add_interceptor`.
    interceptor_id : int
        Value returned by :func:`add_interceptor`.

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
    registry = _get_registry(obj_any, create=False)
    if registry is None:
        return
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None:
        return

    with registry.lock:
        bucket = registry.interceptors.get(registry_key)
        if bucket is None:
            return
        if interceptor_id in bucket:
            del bucket[interceptor_id]
        if not bucket:
            _restore_original_method(obj_any, registry_key)


def del_interceptors(obj, name: str) -> None:
    """Remove all interceptors for *name* on *obj* and restore the original.

    Silently does nothing if *name* is not currently patched on *obj*.

    Parameters
    ----------
    obj : Any
        The same object passed to :func:`add_interceptor`.
    name : str
        The same attribute name passed to :func:`add_interceptor`.

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
    registry = _get_registry(obj_any, create=False)
    if registry is None:
        return
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None:
        return

    with registry.lock:
        if registry_key not in registry.interceptors:
            return
        registry.interceptors[registry_key].clear()
        _restore_original_method(obj_any, registry_key)


def has_interceptors(obj, name: str) -> bool:
    """Return ``True`` if *name* on *obj* has any registered interceptors.

    This check is lock-free — a concurrent mutation may cause the result
    to be momentarily stale, which is acceptable for introspection.

    Parameters
    ----------
    obj : Any
        The same object passed to :func:`add_interceptor`.
    name : str
        The same attribute name passed to :func:`add_interceptor`.

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
    registry = _get_registry(obj_any, create=False)
    if registry is None:
        return False
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None:
        return False
    return bool(registry.interceptors.get(registry_key))


def get_interceptors(obj, name: str) -> list[dict[str, Any]]:
    """Return all interceptors added for *name* on *obj*, in registration order.

    This check is lock-free — the returned snapshot is a point-in-time
    view, safe against concurrent mutation via the internal ``list(...)``
    copy.

    Parameters
    ----------
    obj : Any
        The same object passed to :func:`add_interceptor`.
    name : str
        The same attribute name passed to :func:`add_interceptor`.

    Returns
    -------
    list of dict
        Each dict contains ``id``, ``func``, ``pass_self``, ``pass_args``,
        ``pass_kwargs``, ``is_context_manager``, and ``callorder`` (raw
        registered value — may be a callable).  Empty list if none
        registered.
    """
    obj_any: Any = obj
    registry = _get_registry(obj_any, create=False)
    if registry is None:
        return []
    registry_key = _get_registry_key(obj_any, name)
    if registry_key is None:
        return []
    bucket = registry.interceptors.get(registry_key)
    if not bucket:
        return []

    # Snapshot with list(...) so a concurrent mutation can't raise
    # "dictionary changed size during iteration".
    entries = list(bucket.items())

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
        for iid, (
            func,
            pass_self,
            pass_args,
            pass_kwargs,
            is_cm,
            callorder,
        ) in entries
    ]
