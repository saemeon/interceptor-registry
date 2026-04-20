"""Sync generator semantics: send / throw / close / return-value.

Sync generators use ``yield from`` in the wrapper, which DOES forward
``send`` / ``throw`` / ``close`` correctly — contrast with the async-
generator path (see ``test_async_deep.py`` and the corresponding
``## Bugs found by extended testing`` entry).
"""

from __future__ import annotations

import gc
from contextlib import contextmanager

import pytest

from interceptor_registry import add_interceptor

# ---------------------------------------------------------------------------
# send / throw / close / return
# ---------------------------------------------------------------------------


def test_generator_send_reaches_body():
    events: list = []

    class Obj:
        def m(self):
            received = yield 1
            events.append(("got", received))
            yield 2

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-1)
    gen = obj.m()
    assert next(gen) == 1
    assert gen.send("injected") == 2
    # yield from forwards .send correctly.
    assert ("got", "injected") in events


def test_generator_throw_reaches_body():
    events: list[str] = []

    class Obj:
        def m(self):
            try:
                yield 1
                yield 2
            except RuntimeError:
                events.append("body-caught")
                raise

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-1)
    gen = obj.m()
    assert next(gen) == 1
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("x"))
    assert "body-caught" in events


def test_generator_close_triggers_body_finalizer():
    events: list[str] = []

    class Obj:
        def m(self):
            try:
                yield 1
                yield 2
            finally:
                events.append("body-cleanup")

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    gen = obj.m()
    assert next(gen) == 1
    gen.close()
    assert "body-cleanup" in events


def test_generator_close_triggers_cm_exit():
    events: list[str] = []

    class Obj:
        def m(self):
            yield 1
            yield 2

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    gen = obj.m()
    next(gen)
    gen.close()
    assert events == ["enter", "exit"]


def test_generator_return_value_preserved_by_wrapper():
    """A ``return <value>`` at the end of a generator body sets
    ``StopIteration.value`` to that value (PEP 380).  The wrapper must
    preserve this so callers using the ``result = yield from obj.m()``
    pattern observe the underlying return value unchanged.
    """

    class Obj:
        def m(self):
            yield 1
            yield 2
            return "final"

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)

    def consumer():
        return (yield from obj.m())

    gen = consumer()
    values = []
    try:
        while True:
            values.append(next(gen))
    except StopIteration as stop:
        returned = stop.value
    assert values == [1, 2]
    # The wrapper preserves the inner generator's ``return`` value.
    assert returned == "final"


# ---------------------------------------------------------------------------
# Partial iteration then del gen
# ---------------------------------------------------------------------------


def test_gen_del_triggers_body_cleanup_and_cm_exit():
    events: list[str] = []

    class Obj:
        def m(self):
            try:
                yield 1
                yield 2
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
    gen = obj.m()
    next(gen)
    del gen
    gc.collect()
    # Both cleanup paths must have fired.
    assert "body-cleanup" in events
    assert events[-1] == "exit"


def test_early_break_in_for_loop_exits_cm():
    """Using ``for v in obj.m(): break`` — the generator is finalised
    by GC; CM must exit."""
    events: list[str] = []

    class Obj:
        def m(self):
            for i in range(5):
                events.append(f"body-{i}")
                yield i

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    for v in obj.m():
        if v == 2:
            break
    # The for-loop dropped the generator; gc finalises it.
    gc.collect()
    assert events[-1] == "exit"


# ---------------------------------------------------------------------------
# Hook arguments on generators
# ---------------------------------------------------------------------------


def test_generator_pass_self_forwarded():
    received: list = []

    class Obj:
        def m(self):
            yield 1

    def hook(instance):
        received.append(instance)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_self=True, callorder=-1)
    list(obj.m())
    assert received == [obj]


def test_generator_pass_kwargs_forwarded():
    received: list = []

    class Obj:
        def m(self, **kwargs):
            yield from kwargs.items()

    def hook(**kw):
        received.append(kw)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_kwargs=True, callorder=-1)
    assert dict(obj.m(a=1, b=2)) == {"a": 1, "b": 2}
    assert received == [{"a": 1, "b": 2}]


def test_generator_pre_and_post_hook_fire_exactly_once():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body-1")
            yield 1
            events.append("body-2")
            yield 2

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("post"), callorder=1)

    assert list(obj.m()) == [1, 2]
    assert events == ["pre", "body-1", "body-2", "post"]


def test_generator_cm_exit_after_post_hook():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")
            yield 1

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("post"), callorder=1)
    list(obj.m())
    assert events == ["enter", "body", "post", "exit"]


# ---------------------------------------------------------------------------
# Nested generators
# ---------------------------------------------------------------------------


def test_nested_generator_patched_method_inside_outer():
    events: list[str] = []

    class Obj:
        def outer(self):
            events.append("outer-start")
            yield from self.inner()
            events.append("outer-end")

        def inner(self):
            events.append("inner-body")
            yield 1
            yield 2

    obj = Obj()
    add_interceptor(obj, "outer", lambda: events.append("outer-pre"), callorder=-1)
    add_interceptor(obj, "inner", lambda: events.append("inner-pre"), callorder=-1)

    assert list(obj.outer()) == [1, 2]
    assert events == [
        "outer-pre",
        "outer-start",
        "inner-pre",
        "inner-body",
        "outer-end",
    ]


# ---------------------------------------------------------------------------
# Exception inside body mid-iteration
# ---------------------------------------------------------------------------


def test_generator_body_exception_after_first_yield():
    events: list[str] = []

    class Obj:
        def m(self):
            yield 1
            raise RuntimeError("boom")

    @contextmanager
    def around():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    obj = Obj()
    add_interceptor(obj, "m", around, is_context_manager=True, callorder=-1)
    gen = obj.m()
    assert next(gen) == 1
    with pytest.raises(RuntimeError):
        next(gen)
    # CM may have already exited because the generator raised through
    # it; pin that enter/exit both happened.
    assert "enter" in events
    assert events[-1] == "exit"
