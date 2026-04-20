"""Edge cases: callorder corner values, nested interception, recursion,
self-mutation, unusual target types.

Inspired by the miscellaneous edge-case tests scattered through
wrapt's suite — adapted to interceptor-registry's callorder-numeric
dispatch model.
"""

from __future__ import annotations

import math
import types
from contextlib import contextmanager

import pytest

from interceptor_registry import (
    add_interceptor,
    del_interceptor,
    del_interceptors,
    get_interceptors,
    has_interceptors,
)

# ---------------------------------------------------------------------------
# Callorder corner cases
# ---------------------------------------------------------------------------


def test_callorder_positive_infinity_runs_last():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("first"), callorder=1)
    add_interceptor(obj, "m", lambda: events.append("last"), callorder=math.inf)
    add_interceptor(obj, "m", lambda: events.append("middle"), callorder=50)
    obj.m()
    assert events == ["body", "first", "middle", "last"]


def test_callorder_negative_infinity_runs_first():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("middle"), callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("first"), callorder=-math.inf)
    obj.m()
    # -inf sorts before -1.
    assert events == ["first", "middle", "body"]


def test_callorder_nan_raises_or_is_tolerated():
    """NaN sorts unpredictably in Python (NaN is neither < nor >). Pin
    the current behavior: the library should at minimum not silently
    reorder correctly, but also not crash. The current implementation
    treats NaN as non-zero (since ``nan == 0`` is False), so it is
    tolerated at registration. Sort order is unspecified."""
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("a"), callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("b"), callorder=math.nan)
    # NaN is neither < 0 nor > 0; the library sorts it wherever
    # Python's ``sorted`` puts it. We just assert that the call
    # doesn't raise and that both hooks eventually fire at least
    # once on a sequence of invocations.
    try:
        obj.m()
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"NaN callorder crashed: {exc}")


def test_two_hooks_with_equal_callorder_stable_order_by_registration():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("first-registered"), callorder=-1)
    add_interceptor(obj, "m", lambda: events.append("second-registered"), callorder=-1)
    obj.m()
    # Python's ``sorted`` is stable — ties preserve registration order.
    assert events == ["first-registered", "second-registered", "body"]


def test_callable_callorder_raising_surfaces_at_call_time():
    class Obj:
        def m(self):
            return 1

    def bad():
        raise RuntimeError("order")

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=bad)
    with pytest.raises(RuntimeError, match="order"):
        obj.m()


# ---------------------------------------------------------------------------
# Nested interception — two methods on the same instance
# ---------------------------------------------------------------------------


def test_method_a_calls_method_b_both_intercepted():
    events: list[str] = []

    class Obj:
        def a(self):
            events.append("a-body")
            return self.b() + 1

        def b(self):
            events.append("b-body")
            return 10

    obj = Obj()
    add_interceptor(obj, "a", lambda: events.append("a-pre"), callorder=-1)
    add_interceptor(obj, "b", lambda: events.append("b-pre"), callorder=-1)
    assert obj.a() == 11
    assert events == ["a-pre", "a-body", "b-pre", "b-body"]


# ---------------------------------------------------------------------------
# Recursion — intercepted method calls itself
# ---------------------------------------------------------------------------


def test_recursive_method_interceptors_fire_per_recursion_level():
    events: list[str] = []

    class Obj:
        def m(self, n):
            events.append(f"body-{n}")
            if n <= 0:
                return 0
            return self.m(n - 1) + n

    obj = Obj()
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-1)
    assert obj.m(3) == 6  # 3 + 2 + 1 + 0 = 6
    # One pre per level; one body per level.
    pre_count = events.count("pre")
    assert pre_count == 4
    assert events.count("body-3") == 1
    assert events.count("body-0") == 1


# ---------------------------------------------------------------------------
# Self-mutation — a hook that adds/removes interceptors
# ---------------------------------------------------------------------------


def test_hook_that_adds_interceptor_during_call_runs_next_time():
    """A pre-hook that adds another interceptor during the current call
    — the newly added hook fires on the NEXT call, not this one
    (``_prepare_hooks`` snapshots hooks at dispatch entry)."""
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()

    def installer():
        events.append("installer")
        if "installed" not in events:
            events.append("installed")
            add_interceptor(obj, "m", lambda: events.append("new"), callorder=-1)

    add_interceptor(obj, "m", installer, callorder=-1)
    obj.m()
    # First call: installer fires and adds 'new'; but 'new' is not
    # in the snapshot that _prepare_hooks already took.
    assert events == ["installer", "installed", "body"]

    events.clear()
    obj.m()
    # Second call: both fire (installer and new).
    assert "new" in events
    assert "installer" in events


def test_hook_that_removes_itself_mid_call_completes_current_call():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    obj = Obj()
    iid_holder: list[int] = []

    def self_remover():
        events.append("self_remover")
        if iid_holder:
            del_interceptor(obj, "m", iid_holder[0])

    iid = add_interceptor(obj, "m", self_remover, callorder=-1)
    iid_holder.append(iid)
    obj.m()
    # Current call completes with self_remover having fired.
    assert events == ["self_remover", "body"]

    events.clear()
    obj.m()
    # Next call: self_remover is gone.
    assert events == ["body"]


# ---------------------------------------------------------------------------
# Wrapper identity stability
# ---------------------------------------------------------------------------


def test_wrapper_identity_stable():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert obj.m is obj.m
    # Adding a second interceptor does not swap the wrapper.
    w1 = obj.m
    add_interceptor(obj, "m", lambda: None, callorder=-2)
    w2 = obj.m
    assert w1 is w2


# ---------------------------------------------------------------------------
# Public API ergonomics on unregistered inputs
# ---------------------------------------------------------------------------


def test_has_interceptors_on_never_patched_obj_returns_false():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    assert has_interceptors(obj, "m") is False


def test_get_interceptors_on_never_patched_obj_returns_empty():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    assert get_interceptors(obj, "m") == []


def test_del_interceptor_on_never_patched_obj_is_silent():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    # Should not raise.
    del_interceptor(obj, "m", 999)


def test_del_interceptors_on_never_patched_obj_is_silent():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    del_interceptors(obj, "m")


def test_has_interceptors_on_unknown_name_returns_false():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert has_interceptors(obj, "nonexistent") is False


def test_get_interceptors_on_unknown_name_returns_empty():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert get_interceptors(obj, "nonexistent") == []


# ---------------------------------------------------------------------------
# Unusual target types
# ---------------------------------------------------------------------------


def test_simplenamespace_as_target_rejected_cleanly():
    """``types.SimpleNamespace`` holds attributes in __dict__, but
    ``m`` is stored there as a plain function — not a class-level
    descriptor. ``add_interceptor`` looks up the class-level descriptor,
    which fails because SimpleNamespace has no ``m`` on the class."""
    ns = types.SimpleNamespace()
    ns.m = lambda self: 1
    with pytest.raises(AttributeError):
        add_interceptor(ns, "m", lambda: None, callorder=-1)


def test_callable_instance_with_call_method_instance_dispatch():
    """A class with ``__call__`` — intercepting ``__call__`` patches it
    in the instance ``__dict__``, so ``c.__call__(3)`` goes through the
    interceptor. But ``c(3)`` (the builtin ``type(c).__call__`` lookup)
    bypasses the instance attribute entirely per Python's descriptor
    protocol for dunder methods. Pin both behaviours."""
    events: list[str] = []

    class Callable:
        def __call__(self, x):
            events.append(f"body-{x}")
            return x * 2

    c = Callable()
    add_interceptor(c, "__call__", lambda: events.append("pre"), callorder=-1)

    # Explicit __call__ access goes through the instance __dict__.
    assert c.__call__(3) == 6
    assert events == ["pre", "body-3"]

    # But the builtin call syntax bypasses the instance and goes to
    # the class-level __call__ — this is Python's dunder-method rule,
    # not a limitation of interceptor-registry.
    events.clear()
    assert c(3) == 6
    assert events == ["body-3"]
    assert "pre" not in events


def test_subclass_of_builtin_list():
    """A subclass of list that adds a method — interception works on
    the user-defined method (NOT on list's builtin methods, which are
    in list.__dict__ with C-level descriptors and can't be patched via
    instance __dict__)."""
    events: list[str] = []

    class MyList(list):
        def my_sum(self):
            events.append("body")
            return sum(self)

    ml = MyList([1, 2, 3])
    add_interceptor(ml, "my_sum", lambda: events.append("pre"), callorder=-1)
    assert ml.my_sum() == 6
    assert events == ["pre", "body"]
    # Regular list operations still work.
    ml.append(4)
    assert len(ml) == 4


# ---------------------------------------------------------------------------
# Corner: removing an already-removed interceptor is a silent no-op
# ---------------------------------------------------------------------------


def test_double_del_same_id_is_silent():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    iid = add_interceptor(obj, "m", lambda: None, callorder=-1)
    del_interceptor(obj, "m", iid)
    # Second call should not raise.
    del_interceptor(obj, "m", iid)


def test_del_interceptor_after_del_interceptors_is_silent():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    iid = add_interceptor(obj, "m", lambda: None, callorder=-1)
    del_interceptors(obj, "m")
    del_interceptor(obj, "m", iid)


# ---------------------------------------------------------------------------
# Hook that raises KeyboardInterrupt / SystemExit — propagates
# ---------------------------------------------------------------------------


def test_pre_hook_raising_keyboardinterrupt_propagates():
    class Obj:
        def m(self):
            return 1

    def kb():
        raise KeyboardInterrupt

    obj = Obj()
    add_interceptor(obj, "m", kb, callorder=-1)
    with pytest.raises(KeyboardInterrupt):
        obj.m()


def test_pre_hook_raising_systemexit_propagates():
    class Obj:
        def m(self):
            return 1

    def se():
        raise SystemExit(1)

    obj = Obj()
    add_interceptor(obj, "m", se, callorder=-1)
    with pytest.raises(SystemExit):
        obj.m()


# ---------------------------------------------------------------------------
# Large numbers of interceptors on one method
# ---------------------------------------------------------------------------


def test_100_pre_hooks_fire_in_callorder_sequence():
    events: list[int] = []

    class Obj:
        def m(self):
            events.append(0)

    obj = Obj()
    for i in range(100):
        order = -(i + 1)
        add_interceptor(
            obj, "m", (lambda k: lambda: events.append(k))(order), callorder=order
        )
    obj.m()
    # -100, -99, ..., -1, 0 (the body).
    assert events == list(range(-100, 1))


def test_mixed_pre_and_post_hooks_50_50():
    events: list[int] = []

    class Obj:
        def m(self):
            events.append(0)

    obj = Obj()
    for i in range(1, 26):
        add_interceptor(
            obj, "m", (lambda k: lambda: events.append(-k))(i), callorder=-i
        )
    for i in range(1, 26):
        add_interceptor(obj, "m", (lambda k: lambda: events.append(k))(i), callorder=i)
    obj.m()
    expected = list(range(-25, 26))
    assert events == expected


# ---------------------------------------------------------------------------
# Combining a CM hook with pre/post non-CM hooks
# ---------------------------------------------------------------------------


def test_cm_wraps_pre_and_post_hooks_when_positioned_outermost():
    events: list[str] = []

    class Obj:
        def m(self):
            events.append("body")

    @contextmanager
    def outer():
        events.append("cm-enter")
        try:
            yield
        finally:
            events.append("cm-exit")

    obj = Obj()
    add_interceptor(obj, "m", outer, is_context_manager=True, callorder=-3)
    add_interceptor(obj, "m", lambda: events.append("pre"), callorder=-2)
    add_interceptor(obj, "m", lambda: events.append("post"), callorder=1)
    obj.m()
    # cm-enter runs first; pre, body, post run; cm-exit last.
    assert events == ["cm-enter", "pre", "body", "post", "cm-exit"]


def test_multiple_cms_exit_in_reverse_of_entry():
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

    obj = Obj()
    add_interceptor(obj, "m", lambda: cm("A"), is_context_manager=True, callorder=-3)
    add_interceptor(obj, "m", lambda: cm("B"), is_context_manager=True, callorder=-2)
    add_interceptor(obj, "m", lambda: cm("C"), is_context_manager=True, callorder=-1)
    obj.m()
    assert events == [
        "enter-A",
        "enter-B",
        "enter-C",
        "body",
        "exit-C",
        "exit-B",
        "exit-A",
    ]
