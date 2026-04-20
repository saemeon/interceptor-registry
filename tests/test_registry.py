import asyncio
import concurrent.futures
import gc
import threading
import weakref
from contextlib import contextmanager

import pytest

from interceptor_registry import _registry as _reg
from interceptor_registry import (
    add_interceptor,
    del_interceptor,
    del_interceptors,
    get_interceptors,
    has_interceptors,
)


class Foo:
    def bar(self, x=None):
        return f"bar({x})"

    def baz(self):
        return "baz"

    @classmethod
    def cls_method(cls, x=None):
        return f"cls({x})"

    @staticmethod
    def static_method(x=None):
        return f"static({x})"


# ---------------------------------------------------------------------------
# pre / post hooks
# ---------------------------------------------------------------------------


def test_pre_hook_executes_before_method():
    foo = Foo()
    calls = []
    add_interceptor(foo, "bar", lambda: calls.append("hook"), callorder=-1)
    result = foo.bar()
    assert calls == ["hook"]
    assert result == "bar(None)"


def test_post_hook_executes_after_method():
    foo = Foo()
    calls = []
    add_interceptor(foo, "bar", lambda: calls.append("hook"), callorder=1)
    foo.bar()
    assert calls == ["hook"]


def test_callorder_determines_execution_sequence():
    foo = Foo()
    calls = []
    add_interceptor(foo, "bar", lambda: calls.append("pre_2"), callorder=-1)
    add_interceptor(foo, "bar", lambda: calls.append("pre_1"), callorder=-2)
    add_interceptor(foo, "bar", lambda: calls.append("post_1"), callorder=1)
    add_interceptor(foo, "bar", lambda: calls.append("post_2"), callorder=2)
    foo.bar()
    assert calls == ["pre_1", "pre_2", "post_1", "post_2"]


def test_return_value_is_preserved():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    assert foo.bar("hello") == "bar(hello)"


# ---------------------------------------------------------------------------
# callorder=0 is invalid
# ---------------------------------------------------------------------------


def test_callorder_zero_raises_at_registration():
    foo = Foo()
    with pytest.raises(ValueError, match="callorder=0 is invalid"):
        add_interceptor(foo, "bar", lambda: None, callorder=0)


def test_callable_callorder_zero_raises_at_call_time():
    foo = Foo()
    order = [-1]
    add_interceptor(foo, "bar", lambda: None, callorder=lambda: order[0])
    foo.bar()

    order[0] = 0
    with pytest.raises(ValueError, match="resolved callorder=0"):
        foo.bar()


# ---------------------------------------------------------------------------
# context managers
# ---------------------------------------------------------------------------


def test_context_manager_entered_with_flag():
    foo = Foo()
    calls = []

    @contextmanager
    def around():
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")

    add_interceptor(foo, "bar", around, is_context_manager=True, callorder=-1)
    foo.bar()
    assert calls == ["enter", "exit"]


def test_context_manager_not_entered_without_flag():
    foo = Foo()
    entered = []

    @contextmanager
    def around():
        entered.append(True)
        yield

    add_interceptor(foo, "bar", around, is_context_manager=False, callorder=-1)
    foo.bar()
    assert entered == []


def test_context_manager_exit_called_after_post_hooks():
    foo = Foo()
    calls = []

    @contextmanager
    def cm():
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")

    add_interceptor(foo, "bar", cm, is_context_manager=True, callorder=-1)
    add_interceptor(foo, "bar", lambda: calls.append("post"), callorder=1)
    foo.bar()
    assert calls == ["enter", "post", "exit"]


# ---------------------------------------------------------------------------
# argument forwarding
# ---------------------------------------------------------------------------


def test_pass_self_forwards_instance():
    foo = Foo()
    received = []

    def hook(obj):
        received.append(obj)

    add_interceptor(foo, "bar", hook, pass_self=True, callorder=-1)
    foo.bar()
    assert received == [foo]


def test_pass_args_forwards_positional_args():
    foo = Foo()
    received = []

    def hook(x):
        received.append(x)

    add_interceptor(foo, "bar", hook, pass_args=True, callorder=-1)
    foo.bar("hello")
    assert received == ["hello"]


def test_pass_kwargs_forwards_keyword_args():
    foo = Foo()
    received = []

    def hook(**kw):
        received.append(kw)

    add_interceptor(foo, "bar", hook, pass_kwargs=True, callorder=-1)
    foo.bar(x="hello")
    assert received == [{"x": "hello"}]


# ---------------------------------------------------------------------------
# callable callorder
# ---------------------------------------------------------------------------


def test_callable_callorder_evaluated_at_call_time():
    foo = Foo()
    calls = []
    order = [-2]

    def hook():
        calls.append("hook")

    add_interceptor(foo, "bar", hook, callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]


def test_callable_callorder_can_change_between_calls():
    foo = Foo()
    calls = []
    order = [-1]

    def hook():
        calls.append("hook")

    add_interceptor(foo, "bar", hook, callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]

    order[0] = 1
    foo.bar()
    assert calls == ["hook", "hook"]


# ---------------------------------------------------------------------------
# del_interceptor
# ---------------------------------------------------------------------------


def test_del_interceptor_stops_hook():
    foo = Foo()
    calls = []
    iid = add_interceptor(foo, "bar", lambda: calls.append("hook"), callorder=-1)
    foo.bar()
    assert calls == ["hook"]

    del_interceptor(foo, "bar", iid)
    foo.bar()
    assert calls == ["hook"]


def test_del_interceptor_restores_original_when_last_removed():
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    del_interceptor(foo, "bar", iid)
    assert "bar" not in vars(foo)


def test_del_interceptor_unknown_id_is_silent():
    foo = Foo()
    del_interceptor(foo, "bar", 999)


def test_del_interceptor_unregistered_is_silent():
    foo = Foo()
    del_interceptor(foo, "bar", 0)


def test_del_interceptor_partial_leaves_remaining():
    foo = Foo()
    calls = []
    id_a = add_interceptor(foo, "bar", lambda: calls.append("a"), callorder=-2)
    add_interceptor(foo, "bar", lambda: calls.append("b"), callorder=-1)

    del_interceptor(foo, "bar", id_a)
    foo.bar()
    assert calls == ["b"]
    assert "bar" in vars(foo)


# ---------------------------------------------------------------------------
# del_interceptors
# ---------------------------------------------------------------------------


def test_del_interceptors_stops_all_hooks():
    foo = Foo()
    calls = []
    add_interceptor(foo, "bar", lambda: calls.append("a"), callorder=-2)
    add_interceptor(foo, "bar", lambda: calls.append("b"), callorder=-1)
    foo.bar()
    assert calls == ["a", "b"]

    del_interceptors(foo, "bar")
    foo.bar()
    assert calls == ["a", "b"]


def test_del_interceptors_restores_original():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    del_interceptors(foo, "bar")
    assert "bar" not in vars(foo)


def test_del_interceptors_unregistered_is_silent():
    foo = Foo()
    del_interceptors(foo, "bar")


# ---------------------------------------------------------------------------
# has_interceptors
# ---------------------------------------------------------------------------


def test_has_interceptors_false_when_none_registered():
    foo = Foo()
    assert has_interceptors(foo, "bar") is False


def test_has_interceptors_true_after_add():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    assert has_interceptors(foo, "bar") is True


def test_has_interceptors_false_after_del_all():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptors(foo, "bar")
    assert has_interceptors(foo, "bar") is False


def test_has_interceptors_false_after_last_del():
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptor(foo, "bar", iid)
    assert has_interceptors(foo, "bar") is False


def test_has_interceptors_true_while_partial_remain():
    foo = Foo()
    id_a = add_interceptor(foo, "bar", lambda: None, callorder=-2)
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptor(foo, "bar", id_a)
    assert has_interceptors(foo, "bar") is True


def test_has_interceptors_false_on_unknown_name():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    # 'baz' is never patched on this instance.
    assert has_interceptors(foo, "baz") is False


# ---------------------------------------------------------------------------
# get_interceptors
# ---------------------------------------------------------------------------


def test_get_interceptors_returns_empty_for_unregistered():
    foo = Foo()
    assert get_interceptors(foo, "bar") == []


def test_get_interceptors_returns_registered_entries():
    foo = Foo()

    def hook():
        pass

    iid = add_interceptor(foo, "bar", hook, pass_self=True, callorder=-1)
    result = get_interceptors(foo, "bar")
    assert len(result) == 1
    assert result[0]["id"] == iid
    assert result[0]["func"] is hook
    assert result[0]["pass_self"] is True
    assert result[0]["callorder"] == -1


def test_get_interceptors_empty_after_del_all():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptors(foo, "bar")
    assert get_interceptors(foo, "bar") == []


def test_get_interceptors_reflects_callable_callorder():
    foo = Foo()

    def order_fn():
        return -1

    add_interceptor(foo, "bar", lambda: None, callorder=order_fn)
    assert get_interceptors(foo, "bar")[0]["callorder"] is order_fn


def test_get_interceptors_empty_on_unknown_name():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    # 'baz' is never patched on this instance.
    assert get_interceptors(foo, "baz") == []


# ---------------------------------------------------------------------------
# classmethod
# ---------------------------------------------------------------------------


def test_classmethod_pre_hook():
    foo = Foo()
    calls = []
    add_interceptor(foo, "cls_method", lambda: calls.append("hook"), callorder=-1)
    result = foo.cls_method()
    assert calls == ["hook"]
    assert result == "cls(None)"


def test_classmethod_is_instance_scoped():
    foo1 = Foo()
    foo2 = Foo()
    calls = []
    add_interceptor(foo1, "cls_method", lambda: calls.append("hook"), callorder=-1)
    foo1.cls_method()
    assert calls == ["hook"]
    foo2.cls_method()
    assert calls == ["hook"]


def test_classmethod_restore():
    foo = Foo()
    iid = add_interceptor(foo, "cls_method", lambda: None, callorder=-1)
    assert "cls_method" in vars(foo)
    del_interceptor(foo, "cls_method", iid)
    assert "cls_method" not in vars(foo)
    assert foo.cls_method() == "cls(None)"


# ---------------------------------------------------------------------------
# staticmethod
# ---------------------------------------------------------------------------


def test_staticmethod_pre_hook():
    foo = Foo()
    calls = []
    add_interceptor(foo, "static_method", lambda: calls.append("hook"), callorder=-1)
    result = foo.static_method()
    assert calls == ["hook"]
    assert result == "static(None)"


def test_staticmethod_is_instance_scoped():
    foo1 = Foo()
    foo2 = Foo()
    calls = []
    add_interceptor(foo1, "static_method", lambda: calls.append("hook"), callorder=-1)
    foo1.static_method()
    assert calls == ["hook"]
    foo2.static_method()
    assert calls == ["hook"]


def test_staticmethod_restore():
    foo = Foo()
    iid = add_interceptor(foo, "static_method", lambda: None, callorder=-1)
    assert "static_method" in vars(foo)
    del_interceptor(foo, "static_method", iid)
    assert "static_method" not in vars(foo)
    assert foo.static_method() == "static(None)"


def test_staticmethod_pass_self_gives_instance():
    foo = Foo()
    received = []

    def hook(obj):
        received.append(obj)

    add_interceptor(foo, "static_method", hook, pass_self=True, callorder=-1)
    foo.static_method()
    assert received == [foo]


def test_staticmethod_args_forwarded():
    foo = Foo()
    received = []

    def hook(x):
        received.append(x)

    add_interceptor(foo, "static_method", hook, pass_args=True, callorder=-1)
    foo.static_method("hello")
    assert received == ["hello"]


def test_del_interceptors_on_static():
    foo = Foo()
    add_interceptor(foo, "static_method", lambda: None, callorder=-1)
    del_interceptors(foo, "static_method")
    assert "static_method" not in vars(foo)
    assert foo.static_method() == "static(None)"


def test_get_interceptors_on_static():
    foo = Foo()

    def hook():
        pass

    iid = add_interceptor(foo, "static_method", hook, callorder=-1)
    result = get_interceptors(foo, "static_method")
    assert len(result) == 1
    assert result[0]["id"] == iid
    assert result[0]["func"] is hook


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_hooks_are_instance_specific():
    foo1 = Foo()
    foo2 = Foo()
    calls = []
    add_interceptor(foo1, "bar", lambda: calls.append("foo1"), callorder=-1)
    foo1.bar()
    assert calls == ["foo1"]
    foo2.bar()
    assert calls == ["foo1"]


def test_hooks_are_method_specific():
    foo = Foo()
    calls = []
    add_interceptor(foo, "bar", lambda: calls.append("hook"), callorder=-1)
    foo.baz()
    assert calls == []


def test_restored_method_still_works_correctly():
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptor(foo, "bar", iid)
    assert foo.bar("x") == "bar(x)"


# ---------------------------------------------------------------------------
# Phase 1 item 1 — async / generator / async-generator methods
# ---------------------------------------------------------------------------


async def test_async_method_cm_hook_wraps_body_not_coroutine():
    """is_context_manager=True on an async method must wrap the actual
    awaited body, not the coroutine return value."""
    events: list[str] = []

    class Bar:
        async def do(self):
            await asyncio.sleep(0)
            events.append("body")
            return 42

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    b = Bar()
    add_interceptor(b, "do", around, is_context_manager=True, callorder=-1)
    add_interceptor(b, "do", lambda: events.append("pre"), callorder=-2)
    add_interceptor(b, "do", lambda: events.append("post"), callorder=1)

    result = await b.do()
    assert result == 42
    assert events == ["pre", "enter", "body", "post", "exit"]


async def test_async_method_wrapper_remains_coroutine_function():
    """After patching, ``inspect.iscoroutinefunction`` should still hold."""

    class Bar:
        async def do(self):
            return 7

    b = Bar()
    add_interceptor(b, "do", lambda: None, callorder=-1)
    # The instance attr wrapper itself must be a coroutine function so
    # callers can ``await b.do()``.
    assert asyncio.iscoroutinefunction(vars(b)["do"])
    assert await b.do() == 7


async def test_async_method_exception_propagates_through_cm():
    events: list[str] = []

    class Bar:
        async def boom(self):
            events.append("body")
            raise RuntimeError("kaboom")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        except RuntimeError:
            events.append("caught")
            raise
        finally:
            events.append("exit")

    b = Bar()
    add_interceptor(b, "boom", around, is_context_manager=True, callorder=-1)
    with pytest.raises(RuntimeError, match="kaboom"):
        await b.boom()
    assert events == ["enter", "body", "caught", "exit"]


async def test_async_method_pass_self_args_kwargs():
    received: list = []

    class Bar:
        async def do(self, x, *, y):
            return (x, y)

    def hook(obj, *args, **kwargs):
        received.append((obj, args, kwargs))

    b = Bar()
    add_interceptor(
        b,
        "do",
        hook,
        pass_self=True,
        pass_args=True,
        pass_kwargs=True,
        callorder=-1,
    )
    result = await b.do(1, y=2)
    assert result == (1, 2)
    assert received == [(b, (1,), {"y": 2})]


def test_generator_method_cm_hook_wraps_iteration_not_generator():
    events: list[str] = []

    class Bar:
        def stream(self):
            events.append("body-start")
            yield 1
            events.append("body-middle")
            yield 2
            events.append("body-end")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    b = Bar()
    add_interceptor(b, "stream", around, is_context_manager=True, callorder=-1)
    assert list(b.stream()) == [1, 2]
    assert events == ["enter", "body-start", "body-middle", "body-end", "exit"]


def test_generator_method_early_break_still_exits_cm():
    events: list[str] = []

    class Bar:
        def stream(self):
            for i in range(10):
                events.append(f"body-{i}")
                yield i

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    b = Bar()
    add_interceptor(b, "stream", around, is_context_manager=True, callorder=-1)
    gen = b.stream()
    assert next(gen) == 0
    gen.close()  # simulate early break / gc
    assert "enter" in events
    assert events[-1] == "exit"


def test_generator_pass_args_forwarded():
    received: list = []

    class Bar:
        def stream(self, n):
            yield from range(n)

    def hook(*args):
        received.append(args)

    b = Bar()
    add_interceptor(b, "stream", hook, pass_args=True, callorder=-1)
    assert list(b.stream(3)) == [0, 1, 2]
    assert received == [(3,)]


async def test_async_generator_method_cm_hook_wraps_iteration():
    events: list[str] = []

    class Bar:
        async def stream(self):
            events.append("body-start")
            yield 1
            events.append("body-end")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    b = Bar()
    add_interceptor(b, "stream", around, is_context_manager=True, callorder=-1)
    collected = [v async for v in b.stream()]
    assert collected == [1]
    assert events == ["enter", "body-start", "body-end", "exit"]


async def test_async_generator_pre_post_hooks_fire():
    events: list[str] = []

    class Bar:
        async def stream(self):
            events.append("body")
            yield 1

    b = Bar()
    add_interceptor(b, "stream", lambda: events.append("pre"), callorder=-1)
    add_interceptor(b, "stream", lambda: events.append("post"), callorder=1)
    collected = [v async for v in b.stream()]
    assert collected == [1]
    assert events == ["pre", "body", "post"]


async def test_async_cm_hook_rejected_with_clear_error():
    """Async context-manager hooks are not supported in v0.2."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def acm():
        yield

    class Bar:
        def do(self):
            return 1

    b = Bar()
    add_interceptor(b, "do", acm, is_context_manager=True, callorder=-1)
    with pytest.raises(TypeError, match="context manager"):
        b.do()


# ---------------------------------------------------------------------------
# Phase 1 item 2 — property / non-callable rejection
# ---------------------------------------------------------------------------


def test_add_interceptor_on_readonly_property_raises():
    class FooProp:
        @property
        def value(self):
            return 42

    with pytest.raises(TypeError, match="property"):
        add_interceptor(FooProp(), "value", lambda: None, callorder=-1)


def test_add_interceptor_on_readwrite_property_raises():
    class FooProp:
        _value = 0

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, v):
            self._value = v

    # Read-write properties were previously silently no-op'd; now they raise.
    with pytest.raises(TypeError, match="property"):
        add_interceptor(FooProp(), "value", lambda: None, callorder=-1)


def test_add_interceptor_on_non_callable_class_attr_raises():
    class FooAttr:
        constant = "hello"

    with pytest.raises(TypeError, match="not a supported descriptor kind"):
        add_interceptor(FooAttr(), "constant", lambda: None, callorder=-1)


# ---------------------------------------------------------------------------
# Phase 1 item 2 (extension) — async hook rejection
# ---------------------------------------------------------------------------


def test_async_hook_rejected_at_registration_time():
    class Bar:
        def do(self):
            return 1

    async def async_hook():
        pass

    b = Bar()
    with pytest.raises(TypeError, match=r"[Aa]sync hooks"):
        add_interceptor(b, "do", async_hook, callorder=-1)


def test_async_generator_hook_rejected_at_registration_time():
    class Bar:
        def do(self):
            return 1

    async def agen_hook():
        yield

    b = Bar()
    with pytest.raises(TypeError, match=r"[Aa]sync hooks"):
        add_interceptor(b, "do", agen_hook, callorder=-1)


# ---------------------------------------------------------------------------
# Phase 1 item 3 — thread safety + weak-key-dict state refactor
# ---------------------------------------------------------------------------

# NOTE: concurrency tests below are inherently probabilistic; run locally
# via ``pytest --count=20`` (install pytest-repeat) if you suspect a race.


def test_concurrent_add_interceptor_is_safe():
    foo = Foo()
    calls: list[int] = []
    lock = threading.Lock()

    def make_hook(i):
        def hook():
            with lock:
                calls.append(i)

        return hook

    def worker(i):
        return add_interceptor(foo, "bar", make_hook(i), callorder=-(i + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(worker, range(32)))

    assert len(set(ids)) == 32
    foo.bar()
    assert len(calls) == 32
    assert "bar" in vars(foo)


def test_concurrent_add_and_del_is_safe():
    foo = Foo()
    iids = [
        add_interceptor(foo, "bar", lambda: None, callorder=-(i + 1)) for i in range(16)
    ]

    def remove(iid):
        del_interceptor(foo, "bar", iid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(remove, iids))

    assert not has_interceptors(foo, "bar")
    assert "bar" not in vars(foo)


# ---------------------------------------------------------------------------
# WeakKeyDictionary state refactor — targets no longer carry _registered_*
# ---------------------------------------------------------------------------


def test_target_instance_dict_is_clean():
    """Target should not carry ``_registered_*`` implementation attrs."""
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    instance_attrs = set(vars(foo))
    # Only the wrapper should be installed.
    assert instance_attrs == {"bar"}
    assert not any(attr.startswith("_registered") for attr in instance_attrs)


def test_registry_is_garbage_collected_with_target():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    weak_foo = weakref.ref(foo)
    del foo
    gc.collect()
    assert weak_foo() is None


def test_registry_dropped_when_last_interceptor_removed():
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    assert _reg._get_registry(foo, create=False) is not None
    del_interceptor(foo, "bar", iid)
    assert _reg._get_registry(foo, create=False) is None


def test_non_weakrefable_fallback_works():
    """Objects without ``__weakref__`` slot fall back to attribute storage."""

    class Slim:
        __slots__ = ("name",)

        def __init__(self):
            self.name = "slim"

        def do(self):
            return "done"

    # Slim has __slots__ without __weakref__ → not weakrefable. This test
    # documents that the fallback path works when the class also has
    # __dict__ or allows the fallback attribute. If it doesn't, the
    # library raises cleanly.
    obj = Slim()
    with pytest.raises(TypeError):
        # Either (a) the weakref attempt fails and the attribute fallback
        # can't install __interceptor_registry__ on a strict __slots__
        # class — raising TypeError — or (b) the attribute write succeeds.
        # This class explicitly excludes both, so we expect TypeError.
        weakref.ref(obj)
    # The weakref raises; our code path catches it and tries the
    # attribute-store fallback. A slotted class without a matching slot
    # rejects attribute assignment, so add_interceptor raises TypeError.
    with pytest.raises(TypeError):
        add_interceptor(obj, "do", lambda: None, callorder=-1)


def test_non_weakrefable_with_dict_fallback_succeeds():
    """An object with ``__dict__`` but no ``__weakref__`` still works."""

    class Partial:
        __slots__ = ("__dict__",)

        def do(self):
            return "done"

    obj = Partial()
    # __dict__ slot allows attribute storage but no __weakref__.
    with pytest.raises(TypeError):
        weakref.ref(obj)
    calls = []
    add_interceptor(obj, "do", lambda: calls.append("hit"), callorder=-1)
    assert obj.do() == "done"
    assert calls == ["hit"]


# ---------------------------------------------------------------------------
# Phase 2 item 7 — parametrised matrix across method kinds
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_method_target():
    events: list[str] = []

    class Target:
        def m(self, *args, **kwargs):
            events.append("body")
            return 42

    return Target(), "m", lambda t, *a, **kw: t.m(*a, **kw), events, False


@pytest.fixture
def classmethod_target():
    events: list[str] = []

    class Target:
        @classmethod
        def m(cls, *args, **kwargs):
            events.append("body")
            return 42

    return Target(), "m", lambda t, *a, **kw: t.m(*a, **kw), events, False


@pytest.fixture
def staticmethod_target():
    events: list[str] = []

    class Target:
        @staticmethod
        def m(*args, **kwargs):
            events.append("body")
            return 42

    return Target(), "m", lambda t, *a, **kw: t.m(*a, **kw), events, False


@pytest.fixture
def async_method_target():
    events: list[str] = []

    class Target:
        async def m(self, *args, **kwargs):
            events.append("body")
            return 42

    def invoke(t, *a, **kw):
        return asyncio.get_event_loop().run_until_complete(t.m(*a, **kw))

    return Target(), "m", invoke, events, False


@pytest.fixture
def generator_method_target():
    events: list[str] = []

    class Target:
        def m(self, *args, **kwargs):
            events.append("body")
            yield 42

    def invoke(t, *a, **kw):
        result = list(t.m(*a, **kw))
        return result[0] if result else None

    return Target(), "m", invoke, events, True


@pytest.fixture
def async_generator_method_target():
    events: list[str] = []

    class Target:
        async def m(self, *args, **kwargs):
            events.append("body")
            yield 42

    async def _run(t, *a, **kw):
        result = [v async for v in t.m(*a, **kw)]
        return result[0] if result else None

    def invoke(t, *a, **kw):
        return asyncio.get_event_loop().run_until_complete(_run(t, *a, **kw))

    return Target(), "m", invoke, events, True


ALL_METHOD_KINDS = [
    "sync_method_target",
    "classmethod_target",
    "staticmethod_target",
    "async_method_target",
    "generator_method_target",
    "async_generator_method_target",
]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_pre_hook_fires(request, target_fixture):
    target, name, invoke, events, _is_gen = request.getfixturevalue(target_fixture)
    add_interceptor(target, name, lambda: events.append("pre"), callorder=-1)
    invoke(target)
    assert events == ["pre", "body"]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_post_hook_fires(request, target_fixture):
    target, name, invoke, events, _is_gen = request.getfixturevalue(target_fixture)
    add_interceptor(target, name, lambda: events.append("post"), callorder=1)
    invoke(target)
    assert events == ["body", "post"]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_pre_and_post_hook_fire(request, target_fixture):
    target, name, invoke, events, _is_gen = request.getfixturevalue(target_fixture)
    add_interceptor(target, name, lambda: events.append("pre"), callorder=-1)
    add_interceptor(target, name, lambda: events.append("post"), callorder=1)
    invoke(target)
    assert events == ["pre", "body", "post"]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_cm_hook_wraps_body(request, target_fixture):
    target, name, invoke, events, _is_gen = request.getfixturevalue(target_fixture)

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    add_interceptor(target, name, around, is_context_manager=True, callorder=-1)
    invoke(target)
    assert events == ["enter", "body", "exit"]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_argument_forwarding(request, target_fixture):
    target, name, invoke, _events, _is_gen = request.getfixturevalue(target_fixture)
    received: list = []

    def hook(*args, **kwargs):
        received.append((args, kwargs))

    add_interceptor(target, name, hook, pass_args=True, pass_kwargs=True, callorder=-1)
    invoke(target, 7, flag=True)
    assert received == [((7,), {"flag": True})]


@pytest.mark.parametrize("target_fixture", ALL_METHOD_KINDS)
def test_matrix_exception_propagation(request, target_fixture):
    """A body-raised exception propagates through the ExitStack."""
    target, name, invoke, events, _is_gen = request.getfixturevalue(target_fixture)

    # Replace the body with a raising one by adding a pre-hook that
    # raises instead (the fixture bodies don't raise). This still
    # exercises CM exit under exception.

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    def boom():
        raise RuntimeError("boom")

    add_interceptor(target, name, around, is_context_manager=True, callorder=-2)
    add_interceptor(target, name, boom, callorder=-1)
    with pytest.raises(RuntimeError, match="boom"):
        invoke(target)
    assert events[0] == "enter"
    assert events[-1] == "exit"


# ---------------------------------------------------------------------------
# Defensive / coverage tests
# ---------------------------------------------------------------------------


def test_lookup_raw_descriptor_raises_on_unknown_name():
    foo = Foo()
    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        add_interceptor(foo, "nope", lambda: None, callorder=-1)


def test_async_cm_hook_rejected_in_post_hook_path():
    """Same error when the CM is attached as a post-hook."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def acm():
        yield

    class Bar:
        def do(self):
            return 1

    b = Bar()
    add_interceptor(b, "do", acm, is_context_manager=True, callorder=1)
    with pytest.raises(TypeError, match="context manager"):
        b.do()


def test_async_hook_rejected_at_call_time_if_bypassing_registration():
    """``_trigger_hook`` has a defensive re-check in case an async hook
    slips through (e.g. a hook that was redefined in-place after
    registration — not officially supported but must fail loudly)."""

    class Bar:
        def do(self):
            return 1

    b = Bar()

    def sync_hook():
        pass

    iid = add_interceptor(b, "do", sync_hook, callorder=-1)

    # Tamper directly with the registry to install an async function
    # (the public API rejects these; this simulates a bypass).
    async def async_hook():
        pass

    registry = _reg._get_registry(b, create=False)
    assert registry is not None
    bucket = registry.interceptors
    # Find the key for this wrapper and mutate the entry in place.
    for entries in bucket.values():
        if iid in entries:
            entries[iid] = (async_hook, False, False, False, False, -1)
            break
    with pytest.raises(TypeError, match=r"[Aa]sync hooks"):
        b.do()


def test_get_registry_create_false_returns_none_for_fresh_object():
    foo = Foo()
    assert _reg._get_registry(foo, create=False) is None


def test_del_interceptor_after_registry_dropped_is_silent():
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptor(foo, "bar", iid)
    # Registry has been dropped; another del is a silent no-op.
    del_interceptor(foo, "bar", iid)


def test_del_interceptors_after_registry_dropped_is_silent():
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptors(foo, "bar")
    del_interceptors(foo, "bar")


def test_drop_registry_cleans_fallback_attribute():
    """Covers the non-weakrefable drop path."""

    class Partial:
        __slots__ = ("__dict__",)

        def do(self):
            return 1

    obj = Partial()
    iid = add_interceptor(obj, "do", lambda: None, callorder=-1)
    assert hasattr(obj, _reg._REGISTRY_FALLBACK_ATTR)
    del_interceptor(obj, "do", iid)
    assert not hasattr(obj, _reg._REGISTRY_FALLBACK_ATTR)


def test_non_weakrefable_create_false_returns_none():
    """``_get_registry(obj, create=False)`` on a non-weakrefable fresh
    object must return ``None`` (covers the fallback-branch short-circuit)."""

    class Partial:
        __slots__ = ("__dict__",)

    obj = Partial()
    assert _reg._get_registry(obj, create=False) is None


def test_non_weakrefable_without_dict_direct_store_raises():
    """Calling ``_get_registry`` directly on an object with no
    ``__dict__`` and no ``__weakref__`` must raise a clear
    ``TypeError`` — covers the defensive ``setattr`` failure branch.

    The public API (``add_interceptor``) never actually reaches this
    branch: ``_get_registry_key`` runs first and fails on ``vars(obj)``
    with its own ``TypeError`` for no-dict objects.  This test exercises
    the helper in isolation so the fallback path is still reachable.
    """

    class Locked:
        __slots__ = ("x",)

    obj = Locked()
    with pytest.raises(TypeError, match="not weak-referenceable"):
        _reg._get_registry(obj, create=True)


# ---------------------------------------------------------------------------
# Defensive branches in del_interceptor / del_interceptors / get_interceptors
# ---------------------------------------------------------------------------


def test_del_interceptor_unknown_name_on_partially_patched_obj():
    """Registry exists for *obj* (because ``bar`` is patched) but the
    caller asks about an un-patched name — the call must be a silent
    no-op (covers the ``registry_key is None`` early-return in
    ``del_interceptor``)."""
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    # ``baz`` has no interceptors; this must not raise.
    del_interceptor(foo, "baz", 999)
    # And ``bar`` is still wrapped.
    assert "bar" in vars(foo)


def test_del_interceptors_unknown_name_on_partially_patched_obj():
    """Same shape as above but for ``del_interceptors``."""
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    del_interceptors(foo, "baz")
    assert "bar" in vars(foo)


def test_del_interceptor_with_missing_bucket_is_silent():
    """Covers the defensive ``bucket is None`` branch inside
    ``del_interceptor`` — reachable only by surgically deleting the
    bucket from the registry while the wrapper is still installed."""
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    registry = _reg._get_registry(foo, create=False)
    assert registry is not None
    # Wipe the bucket but keep ``registry.originals`` entry so
    # ``_get_registry_key`` still finds the patched wrapper.
    registry.interceptors.clear()
    # Must not raise.
    del_interceptor(foo, "bar", iid)


def test_del_interceptors_with_missing_bucket_is_silent():
    """Covers ``registry_key not in registry.interceptors`` in
    ``del_interceptors``."""
    foo = Foo()
    add_interceptor(foo, "bar", lambda: None, callorder=-1)
    registry = _reg._get_registry(foo, create=False)
    assert registry is not None
    registry.interceptors.clear()
    # Must not raise.
    del_interceptors(foo, "bar")


def test_get_interceptors_with_empty_bucket_returns_empty():
    """Covers the ``not bucket`` branch in ``get_interceptors`` —
    reachable when the bucket has been surgically emptied but the
    wrapper is still installed."""
    foo = Foo()
    iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    registry = _reg._get_registry(foo, create=False)
    assert registry is not None
    key = _reg._get_registry_key(foo, "bar")
    assert key is not None
    # Empty the bucket without touching ``originals`` / wrapper.
    registry.interceptors[key].clear()
    assert get_interceptors(foo, "bar") == []
    # Cleanup.
    del iid


# ---------------------------------------------------------------------------
# Post-hook context managers (pre-hook CMs were already covered)
# ---------------------------------------------------------------------------


def test_post_hook_context_manager_is_entered():
    """Post-hook with ``is_context_manager=True`` — covers the
    ``stack.enter_context(rv)`` line in ``_run_post_hooks``."""
    foo = Foo()
    events: list[str] = []

    @contextmanager
    def after():
        events.append("post-enter")
        try:
            yield
        finally:
            events.append("post-exit")

    add_interceptor(foo, "bar", after, is_context_manager=True, callorder=1)
    foo.bar()
    # ExitStack closes at the end of the dispatch; ``post-exit`` is
    # guaranteed to fire after ``post-enter``.
    assert events == ["post-enter", "post-exit"]


def test_post_hook_context_manager_rejects_non_cm_return():
    """Symmetric to the pre-hook check — a post-hook CM that returns
    a non-CM must raise ``TypeError``."""
    foo = Foo()

    def bad_post():
        return "not a CM"

    add_interceptor(foo, "bar", bad_post, is_context_manager=True, callorder=1)
    with pytest.raises(TypeError, match="not a synchronous context manager"):
        foo.bar()


# ---------------------------------------------------------------------------
# Concurrent add_interceptor: cover the "lost the race" branch (line 703)
# ---------------------------------------------------------------------------


def test_concurrent_add_interceptor_second_caller_observes_patched_wrapper():
    """Inside the lock, the second caller finds ``_get_registry_key``
    already returns a key (the wrapper is installed) — that covers the
    ``registry_key = existing_key`` branch inside ``add_interceptor``.

    The race is simulated deterministically: we patch the *module-level*
    ``_get_registry_key`` so the first invocation (the outer probe)
    returns ``None`` — forcing the caller to take the slow path — while
    the second invocation (the re-check inside the lock) returns the
    real key so the "already patched" branch executes.
    """
    foo = Foo()
    # First install a wrapper so ``_get_registry_key`` has a real key
    # to return from the inner re-check.
    add_interceptor(foo, "bar", lambda: None, callorder=1)
    real_key = _reg._get_registry_key(foo, "bar")
    assert real_key is not None

    real_fn = _reg._get_registry_key
    call_count = [0]

    def fake_get_registry_key(obj, name):
        call_count[0] += 1
        # First call: pretend nothing is patched (force slow path).
        if call_count[0] == 1:
            return None
        # Subsequent calls: return the truth so the in-lock re-check
        # takes the ``existing_key is not None`` branch.
        return real_fn(obj, name)

    _reg._get_registry_key = fake_get_registry_key  # type: ignore[assignment]
    try:
        iid = add_interceptor(foo, "bar", lambda: None, callorder=-1)
    finally:
        _reg._get_registry_key = real_fn  # type: ignore[assignment]

    # Registration succeeded; bucket grew by one; wrapper was NOT
    # doubled (same key reused).
    assert isinstance(iid, int)
    assert len(get_interceptors(foo, "bar")) == 2
    assert _reg._get_registry_key(foo, "bar") == real_key


def test_prepare_hooks_early_return_when_registry_missing():
    """If the wrapper is invoked but the registry has been cleared
    (e.g. by a concurrent ``del_interceptors`` between the attribute
    lookup and the call), ``_prepare_hooks`` must return an empty
    list instead of raising."""
    # Directly exercise the helper — the path is hard to observe end-
    # to-end without a race, but the defensive branch is real.
    foo = Foo()
    # No registry yet; any registry_key value works.
    assert _reg._prepare_hooks(foo, foo.bar, 12345) == []


def test_restore_original_method_without_registry_is_silent():
    """Covers the defensive ``registry is None`` early return."""
    foo = Foo()
    # No interceptors registered yet; call the internal helper
    # directly.
    _reg._restore_original_method(foo, 12345)
    # Nothing patched, nothing changed.
    assert "bar" not in vars(foo)
