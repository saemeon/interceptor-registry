"""Descriptor edge cases and argument forwarding matrix.

Inspired by wrapt's test_instancemethod.py / test_inner_classmethod.py /
test_outer_classmethod.py / test_inner_staticmethod.py family —
extended to every truth combination of pass_self / pass_args /
pass_kwargs across every method kind.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from interceptor_registry import (
    add_interceptor,
    del_interceptors,
    get_interceptors,
    has_interceptors,
)

# ---------------------------------------------------------------------------
# Inheritance / MRO edge cases
# ---------------------------------------------------------------------------


def test_inherited_method_intercepted_on_child_instance():
    """Parent defines a method; child instance intercepts it."""
    events: list[str] = []

    class Parent:
        def greet(self, name):
            events.append(f"body-{name}")
            return f"hi-{name}"

    class Child(Parent):
        pass

    child = Child()
    add_interceptor(child, "greet", lambda: events.append("pre"), callorder=-1)
    result = child.greet("x")
    assert result == "hi-x"
    assert events == ["pre", "body-x"]


def test_overridden_method_intercepts_child_version_not_parent():
    events: list[str] = []

    class Parent:
        def greet(self):
            events.append("parent-body")
            return "parent"

    class Child(Parent):
        def greet(self):
            events.append("child-body")
            return "child"

    child = Child()
    add_interceptor(child, "greet", lambda: events.append("pre"), callorder=-1)
    result = child.greet()
    assert result == "child"
    assert events == ["pre", "child-body"]


def test_super_call_inside_intercepted_method_still_works():
    """A body that calls super().method() dispatches correctly; parent's
    unpatched method runs."""
    events: list[str] = []

    class Parent:
        def greet(self):
            events.append("parent-body")
            return "parent"

    class Child(Parent):
        def greet(self):
            events.append("child-pre-super")
            r = super().greet()
            events.append("child-post-super")
            return f"child({r})"

    child = Child()
    add_interceptor(child, "greet", lambda: events.append("pre"), callorder=-1)
    result = child.greet()
    assert result == "child(parent)"
    assert events == ["pre", "child-pre-super", "parent-body", "child-post-super"]


def test_classmethod_defined_in_parent_accessed_via_child():
    events: list[str] = []

    class Parent:
        @classmethod
        def make(cls):
            events.append(cls.__name__)
            return cls

    class Child(Parent):
        pass

    child = Child()
    add_interceptor(child, "make", lambda: events.append("pre"), callorder=-1)
    result = child.make()
    # classmethod binds to the instance's class (Child), per Python semantics.
    assert result is Child
    assert events == ["pre", "Child"]


def test_staticmethod_defined_in_parent_accessed_via_child():
    events: list[str] = []

    class Parent:
        @staticmethod
        def util(x):
            events.append(f"body-{x}")
            return x * 2

    class Child(Parent):
        pass

    child = Child()
    add_interceptor(child, "util", lambda: events.append("pre"), callorder=-1)
    assert child.util(3) == 6
    assert events == ["pre", "body-3"]


# ---------------------------------------------------------------------------
# Property rejection (pin the exact behaviour)
# ---------------------------------------------------------------------------


def test_readonly_property_raises_at_registration_time():
    class Obj:
        @property
        def val(self):
            return 1

    with pytest.raises(TypeError, match="property"):
        add_interceptor(Obj(), "val", lambda: None, callorder=-1)


def test_readwrite_property_raises_at_registration_time():
    class Obj:
        _v = 0

        @property
        def val(self):
            return self._v

        @val.setter
        def val(self, v):
            self._v = v

    with pytest.raises(TypeError, match="property"):
        add_interceptor(Obj(), "val", lambda: None, callorder=-1)


def test_deleter_property_raises_at_registration_time():
    class Obj:
        _v = 1

        @property
        def val(self):
            return self._v

        @val.setter
        def val(self, v):
            self._v = v

        @val.deleter
        def val(self):
            del self._v

    with pytest.raises(TypeError, match="property"):
        add_interceptor(Obj(), "val", lambda: None, callorder=-1)


def test_inherited_property_also_raises():
    class Parent:
        @property
        def val(self):
            return 1

    class Child(Parent):
        pass

    with pytest.raises(TypeError, match="property"):
        add_interceptor(Child(), "val", lambda: None, callorder=-1)


# ---------------------------------------------------------------------------
# Argument forwarding — exotic signatures
# ---------------------------------------------------------------------------


def test_positional_only_parameters_forwarded():
    received: list = []

    class Obj:
        def m(self, a, b, /, c):
            return (a, b, c)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_args=True, pass_kwargs=True, callorder=-1)
    assert obj.m(1, 2, 3) == (1, 2, 3)
    assert obj.m(1, 2, c=3) == (1, 2, 3)
    assert received[0] == ((1, 2, 3), {})
    assert received[1] == ((1, 2), {"c": 3})


def test_keyword_only_parameters_forwarded():
    received: list = []

    class Obj:
        def m(self, *, a, b):
            return (a, b)

    def hook(**kw):
        received.append(kw)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_kwargs=True, callorder=-1)
    assert obj.m(a=1, b=2) == (1, 2)
    assert received == [{"a": 1, "b": 2}]


def test_star_args_in_method_signature_forwarded_via_pass_args():
    received: list = []

    class Obj:
        def m(self, *args):
            return args

    def hook(*a):
        received.append(a)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_args=True, callorder=-1)
    assert obj.m(1, 2, 3, 4) == (1, 2, 3, 4)
    assert received == [(1, 2, 3, 4)]


def test_double_star_kwargs_in_method_signature_forwarded_via_pass_kwargs():
    received: list = []

    class Obj:
        def m(self, **kwargs):
            return kwargs

    def hook(**kw):
        received.append(kw)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_kwargs=True, callorder=-1)
    assert obj.m(x=1, y=2, z=3) == {"x": 1, "y": 2, "z": 3}
    assert received == [{"x": 1, "y": 2, "z": 3}]


def test_default_values_preserved_when_no_args_passed():
    received: list = []

    class Obj:
        def m(self, a=10, b=20):
            return (a, b)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_args=True, pass_kwargs=True, callorder=-1)
    assert obj.m() == (10, 20)
    assert received == [((), {})]


def test_annotated_parameters_still_work():
    received: list = []

    class Obj:
        def m(self, a: int, b: str = "x") -> tuple:
            return (a, b)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_args=True, pass_kwargs=True, callorder=-1)
    assert obj.m(1, b="y") == (1, "y")
    assert received == [((1,), {"b": "y"})]


def test_very_long_positional_arg_list_forwarded():
    received: list = []

    class Obj:
        def m(self, *args):
            return sum(args)

    def hook(*a):
        received.append(a)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_args=True, callorder=-1)
    xs = tuple(range(15))
    assert obj.m(*xs) == sum(xs)
    assert received == [xs]


def test_very_long_kwarg_list_forwarded():
    received: list = []

    class Obj:
        def m(self, **kwargs):
            return sum(kwargs.values())

    def hook(**kw):
        received.append(kw)

    obj = Obj()
    add_interceptor(obj, "m", hook, pass_kwargs=True, callorder=-1)
    kw = {f"k{i}": i for i in range(12)}
    assert obj.m(**kw) == sum(kw.values())
    assert received == [kw]


# ---------------------------------------------------------------------------
# pass_self x pass_args x pass_kwargs -- all 8 truth combinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pass_self,pass_args,pass_kwargs",
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
def test_all_pass_flag_combinations_sync(pass_self, pass_args, pass_kwargs):
    received: list = []

    class Obj:
        def m(self, a, *, b):
            return (a, b)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        hook,
        pass_self=pass_self,
        pass_args=pass_args,
        pass_kwargs=pass_kwargs,
        callorder=-1,
    )
    assert obj.m(1, b=2) == (1, 2)

    expected_args: tuple = ()
    expected_kwargs: dict = {}
    if pass_self:
        expected_args = (obj,)
    if pass_args:
        expected_args = (*expected_args, 1)
    if pass_kwargs:
        expected_kwargs = {"b": 2}
    assert received == [(expected_args, expected_kwargs)]


@pytest.mark.parametrize(
    "pass_self,pass_args,pass_kwargs",
    [
        (False, False, False),
        (True, True, True),
        (False, True, False),
        (True, False, True),
    ],
)
def test_all_pass_flag_combinations_classmethod(pass_self, pass_args, pass_kwargs):
    received: list = []

    class Obj:
        @classmethod
        def m(cls, a, *, b):
            return (cls, a, b)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        hook,
        pass_self=pass_self,
        pass_args=pass_args,
        pass_kwargs=pass_kwargs,
        callorder=-1,
    )
    obj.m(1, b=2)
    expected_args: tuple = ()
    expected_kwargs: dict = {}
    if pass_self:
        expected_args = (obj,)
    if pass_args:
        expected_args = (*expected_args, 1)
    if pass_kwargs:
        expected_kwargs = {"b": 2}
    assert received == [(expected_args, expected_kwargs)]


@pytest.mark.parametrize(
    "pass_self,pass_args,pass_kwargs",
    [
        (False, False, False),
        (True, True, True),
        (False, True, False),
        (True, False, True),
    ],
)
def test_all_pass_flag_combinations_staticmethod(pass_self, pass_args, pass_kwargs):
    received: list = []

    class Obj:
        @staticmethod
        def m(a, *, b):
            return (a, b)

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        hook,
        pass_self=pass_self,
        pass_args=pass_args,
        pass_kwargs=pass_kwargs,
        callorder=-1,
    )
    assert obj.m(1, b=2) == (1, 2)
    expected_args: tuple = ()
    expected_kwargs: dict = {}
    if pass_self:
        expected_args = (obj,)
    if pass_args:
        expected_args = (*expected_args, 1)
    if pass_kwargs:
        expected_kwargs = {"b": 2}
    assert received == [(expected_args, expected_kwargs)]


async def test_all_pass_flag_combinations_async_method_full_truth():
    """Assert every truth combination on an async method in one run."""

    class Obj:
        async def m(self, a, *, b):
            return (a, b)

    obj = Obj()

    for pass_self in (False, True):
        for pass_args in (False, True):
            for pass_kwargs in (False, True):
                received: list = []

                def make_hook(bucket):
                    def hook(*a, **kw):
                        bucket.append((a, kw))

                    return hook

                iid = add_interceptor(
                    obj,
                    "m",
                    make_hook(received),
                    pass_self=pass_self,
                    pass_args=pass_args,
                    pass_kwargs=pass_kwargs,
                    callorder=-1,
                )
                assert await obj.m(1, b=2) == (1, 2)
                expected_args: tuple = ()
                expected_kwargs: dict = {}
                if pass_self:
                    expected_args = (obj,)
                if pass_args:
                    expected_args = (*expected_args, 1)
                if pass_kwargs:
                    expected_kwargs = {"b": 2}
                assert received == [(expected_args, expected_kwargs)]
                # cleanup for next iteration
                from interceptor_registry import del_interceptor

                del_interceptor(obj, "m", iid)


def test_pass_flag_combinations_sync_generator():
    received: list = []

    class Obj:
        def m(self, *args, **kwargs):
            yield 1
            yield 2

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        hook,
        pass_self=True,
        pass_args=True,
        pass_kwargs=True,
        callorder=-1,
    )
    assert list(obj.m(7, flag=True)) == [1, 2]
    assert received == [((obj, 7), {"flag": True})]


async def test_pass_flag_combinations_async_generator():
    received: list = []

    class Obj:
        async def m(self, *args, **kwargs):
            yield 1
            yield 2

    def hook(*a, **kw):
        received.append((a, kw))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        hook,
        pass_self=True,
        pass_args=True,
        pass_kwargs=True,
        callorder=-1,
    )
    collected = [v async for v in obj.m(7, flag=True)]
    assert collected == [1, 2]
    assert received == [((obj, 7), {"flag": True})]


# ---------------------------------------------------------------------------
# CM + argument forwarding cross
# ---------------------------------------------------------------------------


def test_cm_hook_receives_pass_args_pass_kwargs():
    """A context-manager hook with pass_args/pass_kwargs receives them
    as arguments to the outer hook call (the one returning the CM)."""
    received: list = []

    class Obj:
        def m(self, a, b=0):
            return a + b

    @contextmanager
    def outer(a, b):
        received.append(("enter", a, b))
        yield
        received.append(("exit", a, b))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        outer,
        pass_args=True,
        pass_kwargs=True,
        is_context_manager=True,
        callorder=-1,
    )
    assert obj.m(3, b=4) == 7
    assert received == [("enter", 3, 4), ("exit", 3, 4)]


def test_cm_hook_with_pass_self_receives_instance():
    received: list = []

    class Obj:
        def m(self):
            return "r"

    @contextmanager
    def outer(instance):
        received.append(("enter", instance))
        yield
        received.append(("exit", instance))

    obj = Obj()
    add_interceptor(
        obj,
        "m",
        outer,
        pass_self=True,
        is_context_manager=True,
        callorder=-1,
    )
    obj.m()
    assert received == [("enter", obj), ("exit", obj)]


# ---------------------------------------------------------------------------
# Sanity: has_interceptors / get_interceptors across inheritance
# ---------------------------------------------------------------------------


def test_has_interceptors_for_inherited_method():
    class Parent:
        def m(self):
            return 1

    class Child(Parent):
        pass

    child = Child()
    assert has_interceptors(child, "m") is False
    add_interceptor(child, "m", lambda: None, callorder=-1)
    assert has_interceptors(child, "m") is True
    del_interceptors(child, "m")
    assert has_interceptors(child, "m") is False


def test_get_interceptors_for_inherited_method():
    class Parent:
        def m(self):
            return 1

    class Child(Parent):
        pass

    child = Child()

    def hook():
        return None

    add_interceptor(child, "m", hook, callorder=-1)
    entries = get_interceptors(child, "m")
    assert len(entries) == 1
    assert entries[0]["func"] is hook


# ---------------------------------------------------------------------------
# Mix of many hooks with varied pass_* flags on one method
# ---------------------------------------------------------------------------


def test_many_varied_hooks_on_single_method():
    events: list = []

    class Obj:
        def m(self, x, *, y):
            events.append(("body", x, y))
            return x + y

    # Pre-hook: pass_self only.
    def pre1(instance):
        events.append(("pre1", instance))

    # Pre-hook: pass_args + pass_kwargs.
    def pre2(*a, **kw):
        events.append(("pre2", a, kw))

    # Post-hook: no args.
    def post1():
        events.append(("post1",))

    # Post-hook with CM: pass_self + pass_args.
    @contextmanager
    def around(instance, x):
        events.append(("enter", instance, x))
        yield
        events.append(("exit", instance, x))

    obj = Obj()
    add_interceptor(obj, "m", pre1, pass_self=True, callorder=-2)
    add_interceptor(obj, "m", pre2, pass_args=True, pass_kwargs=True, callorder=-1)
    add_interceptor(
        obj,
        "m",
        around,
        pass_self=True,
        pass_args=True,
        is_context_manager=True,
        callorder=-3,
    )
    add_interceptor(obj, "m", post1, callorder=1)

    assert obj.m(2, y=3) == 5
    # enter first (callorder=-3); pre1 (-2); pre2 (-1); body; post1 (1); exit
    assert events[0] == ("enter", obj, 2)
    assert events[1] == ("pre1", obj)
    assert events[2] == ("pre2", (2,), {"y": 3})
    assert events[3] == ("body", 2, 3)
    assert events[4] == ("post1",)
    assert events[5] == ("exit", obj, 2)


# ---------------------------------------------------------------------------
# Sync-wrapper-returns-async-function smoke (not supported but should not crash)
# ---------------------------------------------------------------------------


def test_sync_method_returning_coroutine_is_left_alone():
    """A *sync* method whose body happens to return a coroutine is
    treated as a plain sync method (the wrapper does not await)."""

    class Obj:
        def m(self):
            async def coro():
                return 7

            return coro()

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    result = obj.m()
    assert asyncio.iscoroutine(result)
    # Drain the coroutine to avoid unawaited-coroutine warning noise.
    result.close()
