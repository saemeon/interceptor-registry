from contextlib import contextmanager

import pytest

from interceptor_registry import (
    deregister_all_method_interceptors,
    deregister_method_interceptor,
    register_method_interceptor,
)


class Foo:
    def bar(self, x=None):
        return f"bar({x})"

    def baz(self):
        return "baz"


# --- pre/post hooks ---


def test_pre_hook_executes_before_method():
    foo = Foo()
    calls = []
    register_method_interceptor(foo.bar, lambda: calls.append("hook"), callorder=-1)
    result = foo.bar()
    assert calls == ["hook"]
    assert result == "bar(None)"


def test_post_hook_executes_after_method():
    foo = Foo()
    calls = []
    register_method_interceptor(foo.bar, lambda: calls.append("hook"), callorder=1)
    foo.bar()
    assert calls == ["hook"]


def test_callorder_determines_execution_sequence():
    foo = Foo()
    calls = []
    register_method_interceptor(foo.bar, lambda: calls.append("pre_2"), callorder=-1)
    register_method_interceptor(foo.bar, lambda: calls.append("pre_1"), callorder=-2)
    register_method_interceptor(foo.bar, lambda: calls.append("post_1"), callorder=1)
    register_method_interceptor(foo.bar, lambda: calls.append("post_2"), callorder=2)
    foo.bar()
    assert calls == ["pre_1", "pre_2", "post_1", "post_2"]


def test_return_value_is_preserved():
    foo = Foo()
    register_method_interceptor(foo.bar, lambda: None, callorder=-1)
    assert foo.bar("hello") == "bar(hello)"


# --- context manager ---


def test_context_manager_wraps_method():
    foo = Foo()
    calls = []

    @contextmanager
    def around():
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")

    register_method_interceptor(foo.bar, around, callorder=-1)
    foo.bar()
    assert calls == ["enter", "exit"]


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

    register_method_interceptor(foo.bar, cm, callorder=-1)
    register_method_interceptor(foo.bar, lambda: calls.append("post"), callorder=1)
    foo.bar()
    assert calls == ["enter", "post", "exit"]


# --- argument forwarding ---


def test_pass_self_forwards_instance():
    foo = Foo()
    received = []
    register_method_interceptor(foo.bar, lambda obj: received.append(obj), pass_self=True, callorder=-1)
    foo.bar()
    assert received == [foo]


def test_pass_args_forwards_positional_args():
    foo = Foo()
    received = []
    register_method_interceptor(foo.bar, lambda x: received.append(x), pass_args=True, callorder=-1)
    foo.bar("hello")
    assert received == ["hello"]


def test_pass_kwargs_forwards_keyword_args():
    foo = Foo()
    received = []
    register_method_interceptor(foo.bar, lambda **kw: received.append(kw), pass_kwargs=True, callorder=-1)
    foo.bar(x="hello")
    assert received == [{"x": "hello"}]


# --- callable callorder ---


def test_callable_callorder_evaluated_at_call_time():
    foo = Foo()
    calls = []
    order = [-2]
    register_method_interceptor(foo.bar, lambda: calls.append("hook"), callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]  # order -2 → pre-hook


def test_callable_callorder_can_change_between_calls():
    foo = Foo()
    calls = []
    order = [-1]

    register_method_interceptor(foo.bar, lambda: calls.append("hook"), callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]

    order[0] = 1
    foo.bar()
    assert calls == ["hook", "hook"]


# --- deregister ---


def test_deregister_stops_hook():
    foo = Foo()
    calls = []
    interceptor_id = register_method_interceptor(foo.bar, lambda: calls.append("hook"), callorder=-1)
    foo.bar()
    assert calls == ["hook"]

    deregister_method_interceptor(foo.bar, interceptor_id)
    foo.bar()
    assert calls == ["hook"]  # no second call


def test_deregister_restores_original_method_when_last_hook_removed():
    foo = Foo()
    interceptor_id = register_method_interceptor(foo.bar, lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    deregister_method_interceptor(foo.bar, interceptor_id)
    assert "bar" not in vars(foo)


def test_deregister_unknown_id_is_silent():
    foo = Foo()
    deregister_method_interceptor(foo.bar, 999)  # no error


def test_deregister_on_unregistered_object_is_silent():
    foo = Foo()
    deregister_method_interceptor(foo.bar, 0)  # no error


def test_deregister_partial_leaves_remaining_hooks():
    foo = Foo()
    calls = []
    id_a = register_method_interceptor(foo.bar, lambda: calls.append("a"), callorder=-2)
    register_method_interceptor(foo.bar, lambda: calls.append("b"), callorder=-1)

    deregister_method_interceptor(foo.bar, id_a)
    foo.bar()
    assert calls == ["b"]
    assert "bar" in vars(foo)  # still patched


# --- deregister_all ---


def test_deregister_all_stops_all_hooks():
    foo = Foo()
    calls = []
    register_method_interceptor(foo.bar, lambda: calls.append("a"), callorder=-2)
    register_method_interceptor(foo.bar, lambda: calls.append("b"), callorder=-1)
    foo.bar()
    assert calls == ["a", "b"]

    deregister_all_method_interceptors(foo.bar)
    foo.bar()
    assert calls == ["a", "b"]  # no new calls


def test_deregister_all_restores_original_method():
    foo = Foo()
    register_method_interceptor(foo.bar, lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    deregister_all_method_interceptors(foo.bar)
    assert "bar" not in vars(foo)


def test_deregister_all_on_unregistered_method_is_silent():
    foo = Foo()
    deregister_all_method_interceptors(foo.bar)  # no error


def test_deregister_all_on_unregistered_object_is_silent():
    foo = Foo()
    deregister_all_method_interceptors(foo.bar)  # no error


# --- isolation ---


def test_hooks_are_instance_specific():
    foo1 = Foo()
    foo2 = Foo()
    calls = []

    register_method_interceptor(foo1.bar, lambda: calls.append("foo1"), callorder=-1)
    foo1.bar()
    assert calls == ["foo1"]

    foo2.bar()
    assert calls == ["foo1"]  # foo2 unaffected


def test_hooks_are_method_specific():
    foo = Foo()
    calls = []

    register_method_interceptor(foo.bar, lambda: calls.append("bar_hook"), callorder=-1)
    foo.baz()
    assert calls == []  # baz unaffected


def test_restored_method_still_works_correctly():
    foo = Foo()
    interceptor_id = register_method_interceptor(foo.bar, lambda: None, callorder=-1)
    deregister_method_interceptor(foo.bar, interceptor_id)
    assert foo.bar("x") == "bar(x)"
