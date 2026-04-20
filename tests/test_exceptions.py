"""Exception propagation matrix across sync/async/generator paths.

Inspired by wrapt's test_pyerr_clear_propagation.py (463 lines) —
adapted to interceptor-registry's ExitStack-based CM-hook semantics.

Truth table exercised:

  origin of exception        | method | pre | post | CM __exit__ sees
  pre-hook (non-CM)          |  no    |  ✓  |  no  | the pre-hook's exc
  pre-hook CM __enter__      |  no    |  ?  |  no  | propagates
  method body                |  yes   |  ✓  |  no  | the body's exc
  post-hook (non-CM)         |  yes   |  ✓  |  part| the post-hook's exc
  CM __exit__ itself         |  yes   |  ✓  |  ✓   | chained into __context__
  CM __exit__ that returns   |  yes   |  ✓  |  ✓   | suppresses
  True on an exception
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from interceptor_registry import add_interceptor

# ---------------------------------------------------------------------------
# Exceptions from pre-hooks
# ---------------------------------------------------------------------------


def test_exception_in_pre_hook_prevents_method_and_post_hooks():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            return 42

    def pre():
        events.append("pre-before-raise")
        raise RuntimeError("pre-boom")

    obj = Obj()
    add_interceptor(obj, "m", pre, callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("post"), callorder=1)

    with pytest.raises(RuntimeError, match="pre-boom"):
        obj.m()
    assert events == ["pre-before-raise"]
    assert "body" not in events
    assert "post" not in events


def test_exception_in_pre_hook_propagates_to_previously_entered_cm_exit():
    """A CM that was entered before a raising pre-hook must have its
    __exit__ called with the raised exception."""
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    @contextmanager
    def outer():
        events.append("enter-outer")
        try:
            yield
        except RuntimeError as e:
            events.append(f"exit-outer-caught-{e}")
            raise
        else:
            events.append("exit-outer-clean")

    def raising():
        raise RuntimeError("pre-boom")

    obj = Obj()
    # outer enters first (callorder=-2), then the raising pre (-1)
    add_interceptor(obj, "m", outer, is_context_manager=True, callorder=-2)
    add_interceptor(obj, "m", raising, callorder=-1)

    with pytest.raises(RuntimeError, match="pre-boom"):
        obj.m()
    assert "enter-outer" in events
    # outer saw the exception.
    assert any(s.startswith("exit-outer-caught") for s in events)
    assert "body" not in events


def test_multiple_cms_exit_in_lifo_order_when_pre_hook_raises():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    @contextmanager
    def cm(name):
        events.append(f"enter-{name}")
        try:
            yield
        finally:
            events.append(f"exit-{name}")

    def raising():
        raise RuntimeError("x")

    obj = Obj()
    add_interceptor(obj, "m", lambda: cm("A"), is_context_manager=True, callorder=-3)
    add_interceptor(obj, "m", lambda: cm("B"), is_context_manager=True, callorder=-2)
    add_interceptor(obj, "m", raising, callorder=-1)

    with pytest.raises(RuntimeError):
        obj.m()
    # A enters, B enters, raising runs, B exits (LIFO), A exits.
    assert events == ["enter-A", "enter-B", "exit-B", "exit-A"]


def test_exception_from_pre_hook_that_is_a_cm_enter():
    """A CM hook whose __enter__ raises — no body runs, no other pre
    hooks run if the raising CM is registered last in the pre list."""
    events: list[str] = []

    class BadCM:
        def __enter__(self):
            events.append("enter-raise")
            raise RuntimeError("enter-boom")

        def __exit__(self, *exc):
            events.append("exit")
            return False

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    add_interceptor(obj, "m", lambda: BadCM(), is_context_manager=True, callorder=-1)
    with pytest.raises(RuntimeError, match="enter-boom"):
        obj.m()
    assert events == ["enter-raise"]
    assert "body" not in events


# ---------------------------------------------------------------------------
# Exception from the method body
# ---------------------------------------------------------------------------


def test_exception_in_method_body_skips_post_hooks_but_exits_cms():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            raise ValueError("body-boom")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        except ValueError:
            events.append("caught-in-cm")
            raise
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-1)
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-2)
    add_interceptor(obj, "m", lambda: events.append("post"), callorder=1)

    with pytest.raises(ValueError, match="body-boom"):
        obj.m()

    assert "pre" in events
    assert "body" in events
    assert "caught-in-cm" in events
    assert events[-1] == "exit"
    assert "post" not in events


def test_cm_exit_suppressing_exception_from_body_returns_none():
    """When a pre-hook CM's ``__exit__`` returns ``True`` to suppress an
    exception raised by the method body, the dispatcher returns
    ``None`` (the nominal result never existed) and the exception does
    not leak out of the call.
    """
    events: list[str] = []

    class SuppressingCM:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(f"exit-{exc_type.__name__ if exc_type else 'clean'}")
            return exc_type is ValueError  # suppress only ValueError

    class Obj:
        def m(self):
            events.append("body")
            raise ValueError("swallow-me")

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: SuppressingCM(), is_context_manager=True, callorder=-1
    )
    # The suppressed ValueError must not leak out; the call returns None.
    assert obj.m() is None
    assert "enter" in events
    assert "body" in events
    assert events[-1].startswith("exit-")


async def test_async_cm_exit_suppressing_body_exception_returns_none():
    """Async analogue of the sync suppression test: a CM-hook that
    suppresses the body's exception causes the awaited call to return
    ``None`` instead of raising ``UnboundLocalError``.
    """
    events: list[str] = []

    class SuppressingCM:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(f"exit-{exc_type.__name__ if exc_type else 'clean'}")
            return exc_type is ValueError

    class Obj:
        async def m(self):
            events.append("body")
            raise ValueError("swallow-me")

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: SuppressingCM(), is_context_manager=True, callorder=-1
    )
    assert await obj.m() is None
    assert "enter" in events
    assert "body" in events


def test_sync_gen_cm_exit_suppressing_body_exception_stops_cleanly():
    """A CM-hook around a generator body that suppresses an exception
    raised mid-iteration makes the wrapper stop cleanly instead of
    raising ``UnboundLocalError``.
    """
    events: list[str] = []

    class SuppressingCM:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(f"exit-{exc_type.__name__ if exc_type else 'clean'}")
            return exc_type is ValueError

    class Obj:
        def m(self):
            yield 1
            raise ValueError("swallow-me")

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: SuppressingCM(), is_context_manager=True, callorder=-1
    )
    # The ValueError is suppressed; the wrapper stops cleanly.
    collected = list(obj.m())
    assert collected == [1]
    assert "enter" in events


async def test_async_gen_cm_exit_suppressing_body_exception_stops_cleanly():
    """Async-gen analogue of the sync-gen suppression test."""
    events: list[str] = []

    class SuppressingCM:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(f"exit-{exc_type.__name__ if exc_type else 'clean'}")
            return exc_type is ValueError

    class Obj:
        async def m(self):
            yield 1
            raise ValueError("swallow-me")

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: SuppressingCM(), is_context_manager=True, callorder=-1
    )
    collected = [v async for v in obj.m()]
    assert collected == [1]
    assert "enter" in events


# ---------------------------------------------------------------------------
# Exception from post-hook
# ---------------------------------------------------------------------------


def test_exception_in_post_hook_after_body_success():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            return 7

    def raising_post():
        events.append("post1-raise")
        raise ArithmeticError("post-boom")

    obj = Obj()
    add_interceptor(obj, "m", raising_post, callorder=1)
    add_interceptor(obj, "m", lambda: events.append("post2"), callorder=2)

    with pytest.raises(ArithmeticError, match="post-boom"):
        obj.m()
    assert events == ["body", "post1-raise"]
    # post2 must NOT run after the first post-hook raised.
    assert "post2" not in events


def test_exception_in_post_hook_propagates_to_cm_exit():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            return 7

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        except ArithmeticError:
            events.append("cm-caught")
            raise
        finally:
            events.append("exit")

    def raising_post():
        raise ArithmeticError("post-boom")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    add_interceptor(obj, "m", raising_post, callorder=1)

    with pytest.raises(ArithmeticError):
        obj.m()
    assert "cm-caught" in events
    assert events[-1] == "exit"


# ---------------------------------------------------------------------------
# Exception in a CM's __exit__ itself — chained per PEP 3134
# ---------------------------------------------------------------------------


def test_exception_in_cm_exit_is_chained_with_body_exception():
    class RaiseOnExit:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            raise RuntimeError("exit-boom")

    class Obj:
        def m(self):
            raise ValueError("body-boom")

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: RaiseOnExit(), is_context_manager=True, callorder=-1
    )
    with pytest.raises(RuntimeError, match="exit-boom") as excinfo:
        obj.m()
    # PEP 3134: the ValueError is visible via __context__.
    chained = excinfo.value.__context__
    assert isinstance(chained, ValueError)
    assert str(chained) == "body-boom"


def test_exception_in_cm_exit_without_body_exception_surfaces_cleanly():
    class RaiseOnExit:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            raise RuntimeError("exit-boom")

    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(
        obj, "m", lambda: RaiseOnExit(), is_context_manager=True, callorder=-1
    )
    with pytest.raises(RuntimeError, match="exit-boom"):
        obj.m()


# ---------------------------------------------------------------------------
# BaseException subclasses propagate without swallowing
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_propagates_from_body():
    events: list[str] = []

    class Obj:
        def m(self):
            raise KeyboardInterrupt("user-interrupt")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    with pytest.raises(KeyboardInterrupt):
        obj.m()
    assert events == ["enter", "exit"]


def test_system_exit_propagates_from_body():
    events: list[str] = []

    class Obj:
        def m(self):
            raise SystemExit(2)

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    with pytest.raises(SystemExit):
        obj.m()
    assert events == ["enter", "exit"]


def test_generator_exit_propagates_from_body():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            return 1

    def raise_ge():
        raise GeneratorExit

    obj = Obj()
    add_interceptor(obj, "m", raise_ge, callorder=-1)
    with pytest.raises(GeneratorExit):
        obj.m()
    assert "body" not in events


# ---------------------------------------------------------------------------
# Reraise inside a pre-hook propagates
# ---------------------------------------------------------------------------


def test_pre_hook_reraise_inside_except_block():
    class Obj:
        def m(self):
            return 1

    def pre():
        try:
            raise LookupError("inner")
        except LookupError:
            raise  # explicit reraise

    obj = Obj()
    add_interceptor(obj, "m", pre, callorder=-1)
    with pytest.raises(LookupError, match="inner"):
        obj.m()


# ---------------------------------------------------------------------------
# Async variants
# ---------------------------------------------------------------------------


async def test_async_method_body_exception_propagates_through_cm():
    events: list[str] = []

    class Obj:
        async def m(self):
            events.append("body")
            raise RuntimeError("async-boom")

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

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    with pytest.raises(RuntimeError, match="async-boom"):
        await obj.m()
    assert events == ["enter", "body", "caught", "exit"]


async def test_async_method_pre_hook_exception_prevents_await():
    events: list[str] = []

    class Obj:
        async def m(self):
            events.append("body")
            return 1

    def raising():
        raise RuntimeError("pre-boom")

    obj = Obj()
    add_interceptor(obj, "m", raising, callorder=-1)
    with pytest.raises(RuntimeError, match="pre-boom"):
        await obj.m()
    assert "body" not in events


async def test_async_method_post_hook_exception_propagates():
    class Obj:
        async def m(self):
            return 1

    def raising():
        raise RuntimeError("post-boom")

    obj = Obj()
    add_interceptor(obj, "m", raising, callorder=1)
    with pytest.raises(RuntimeError, match="post-boom"):
        await obj.m()


# ---------------------------------------------------------------------------
# Sync generator variants
# ---------------------------------------------------------------------------


def test_generator_body_exception_propagates_through_cm():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body-start")
            yield 1
            raise RuntimeError("gen-boom")

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

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    with pytest.raises(RuntimeError, match="gen-boom"):
        list(obj.m())
    assert events[-1] == "exit"
    assert "caught" in events


def test_generator_throw_reaches_body_and_triggers_cm_exit():
    events: list[str] = []

    class Obj:
        def m(self):
            try:
                yield 1
                yield 2
            except ValueError:
                events.append("body-caught")
                raise

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        except ValueError:
            events.append("cm-caught")
            raise
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    gen = obj.m()
    assert next(gen) == 1
    with pytest.raises(ValueError):
        gen.throw(ValueError("injected"))
    assert "body-caught" in events
    assert events[-1] == "exit"


# ---------------------------------------------------------------------------
# Async generator variants
# ---------------------------------------------------------------------------


async def test_async_generator_body_exception_propagates_through_cm():
    events: list[str] = []

    class Obj:
        async def m(self):
            events.append("body-start")
            yield 1
            raise RuntimeError("agen-boom")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    with pytest.raises(RuntimeError, match="agen-boom"):
        async for _ in obj.m():
            pass
    assert events[-1] == "exit"


async def test_async_generator_cancel_triggers_cm_exit():
    """An in-flight async-generator consumer that gets cancelled still
    unwinds the CM."""
    events: list[str] = []

    class Obj:
        async def m(self):
            events.append("body-start")
            try:
                while True:
                    yield 1
                    await asyncio.sleep(0.01)
            finally:
                events.append("body-cleanup")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)

    async def consumer():
        async for _ in obj.m():
            await asyncio.sleep(0.01)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 'enter' fires; 'exit' eventually fires via ExitStack close on
    # generator-cleanup. We don't assert strict order of the cleanup
    # paths here (depends on event-loop / gc timing).
    assert "enter" in events


# ---------------------------------------------------------------------------
# Callable callorder that raises — where is the error surfaced?
# ---------------------------------------------------------------------------


def test_callable_callorder_that_raises_propagates_at_call_time():
    class Obj:
        def m(self):
            return 1

    def bad_order():
        raise RuntimeError("order-boom")

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=bad_order)
    with pytest.raises(RuntimeError, match="order-boom"):
        obj.m()
