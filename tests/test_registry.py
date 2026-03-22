from contextlib import contextmanager

import pytest

from interceptor_registry import get_interceptors, register, remove, remove_all


class Foo:
    def bar(self, x=None):
        return f"bar({x})"

    def baz(self):
        return "baz"


# ---------------------------------------------------------------------------
# pre / post hooks
# ---------------------------------------------------------------------------


def test_pre_hook_executes_before_method():
    foo = Foo()
    calls = []
    register(foo.bar, lambda: calls.append("hook"), callorder=-1)
    result = foo.bar()
    assert calls == ["hook"]
    assert result == "bar(None)"


def test_post_hook_executes_after_method():
    foo = Foo()
    calls = []
    register(foo.bar, lambda: calls.append("hook"), callorder=1)
    foo.bar()
    assert calls == ["hook"]


def test_callorder_determines_execution_sequence():
    foo = Foo()
    calls = []
    register(foo.bar, lambda: calls.append("pre_2"), callorder=-1)
    register(foo.bar, lambda: calls.append("pre_1"), callorder=-2)
    register(foo.bar, lambda: calls.append("post_1"), callorder=1)
    register(foo.bar, lambda: calls.append("post_2"), callorder=2)
    foo.bar()
    assert calls == ["pre_1", "pre_2", "post_1", "post_2"]


def test_return_value_is_preserved():
    foo = Foo()
    register(foo.bar, lambda: None, callorder=-1)
    assert foo.bar("hello") == "bar(hello)"


# ---------------------------------------------------------------------------
# callorder=0 is invalid
# ---------------------------------------------------------------------------


def test_callorder_zero_raises_at_registration():
    foo = Foo()
    with pytest.raises(ValueError, match="callorder=0 is invalid"):
        register(foo.bar, lambda: None, callorder=0)


def test_callable_callorder_zero_raises_at_call_time():
    foo = Foo()
    order = [-1]
    register(foo.bar, lambda: None, callorder=lambda: order[0])
    foo.bar()  # fine at order=-1

    order[0] = 0
    with pytest.raises(ValueError, match="resolved callorder=0"):
        foo.bar()


# ---------------------------------------------------------------------------
# context managers — explicit is_context_manager flag
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

    register(foo.bar, around, is_context_manager=True, callorder=-1)
    foo.bar()
    assert calls == ["enter", "exit"]


def test_context_manager_not_entered_without_flag():
    """Without is_context_manager=True the return value is simply ignored."""
    foo = Foo()
    entered = []

    @contextmanager
    def around():
        entered.append(True)
        yield

    register(foo.bar, around, is_context_manager=False, callorder=-1)
    foo.bar()
    assert entered == []  # generator created but never entered


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

    register(foo.bar, cm, is_context_manager=True, callorder=-1)
    register(foo.bar, lambda: calls.append("post"), callorder=1)
    foo.bar()
    assert calls == ["enter", "post", "exit"]


# ---------------------------------------------------------------------------
# argument forwarding
# ---------------------------------------------------------------------------


def test_pass_self_forwards_instance():
    foo = Foo()
    received = []
    register(foo.bar, lambda obj: received.append(obj), pass_self=True, callorder=-1)
    foo.bar()
    assert received == [foo]


def test_pass_args_forwards_positional_args():
    foo = Foo()
    received = []
    register(foo.bar, lambda x: received.append(x), pass_args=True, callorder=-1)
    foo.bar("hello")
    assert received == ["hello"]


def test_pass_kwargs_forwards_keyword_args():
    foo = Foo()
    received = []
    register(foo.bar, lambda **kw: received.append(kw), pass_kwargs=True, callorder=-1)
    foo.bar(x="hello")
    assert received == [{"x": "hello"}]


# ---------------------------------------------------------------------------
# callable callorder
# ---------------------------------------------------------------------------


def test_callable_callorder_evaluated_at_call_time():
    foo = Foo()
    calls = []
    order = [-2]
    register(foo.bar, lambda: calls.append("hook"), callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]  # order -2 → pre-hook


def test_callable_callorder_can_change_between_calls():
    foo = Foo()
    calls = []
    order = [-1]
    register(foo.bar, lambda: calls.append("hook"), callorder=lambda: order[0])
    foo.bar()
    assert calls == ["hook"]

    order[0] = 1
    foo.bar()
    assert calls == ["hook", "hook"]


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_stops_hook():
    foo = Foo()
    calls = []
    iid = register(foo.bar, lambda: calls.append("hook"), callorder=-1)
    foo.bar()
    assert calls == ["hook"]

    remove(foo.bar, iid)
    foo.bar()
    assert calls == ["hook"]  # no second call


def test_remove_restores_original_method_when_last_hook_removed():
    foo = Foo()
    iid = register(foo.bar, lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    remove(foo.bar, iid)
    assert "bar" not in vars(foo)


def test_remove_unknown_id_is_silent():
    foo = Foo()
    remove(foo.bar, 999)


def test_remove_on_unregistered_object_is_silent():
    foo = Foo()
    remove(foo.bar, 0)


def test_remove_partial_leaves_remaining_hooks():
    foo = Foo()
    calls = []
    id_a = register(foo.bar, lambda: calls.append("a"), callorder=-2)
    register(foo.bar, lambda: calls.append("b"), callorder=-1)

    remove(foo.bar, id_a)
    foo.bar()
    assert calls == ["b"]
    assert "bar" in vars(foo)  # still patched


# ---------------------------------------------------------------------------
# remove_all
# ---------------------------------------------------------------------------


def test_remove_all_stops_all_hooks():
    foo = Foo()
    calls = []
    register(foo.bar, lambda: calls.append("a"), callorder=-2)
    register(foo.bar, lambda: calls.append("b"), callorder=-1)
    foo.bar()
    assert calls == ["a", "b"]

    remove_all(foo.bar)
    foo.bar()
    assert calls == ["a", "b"]  # no new calls


def test_remove_all_restores_original_method():
    foo = Foo()
    register(foo.bar, lambda: None, callorder=-1)
    assert "bar" in vars(foo)

    remove_all(foo.bar)
    assert "bar" not in vars(foo)


def test_remove_all_on_unregistered_method_is_silent():
    foo = Foo()
    remove_all(foo.bar)


def test_remove_all_on_unregistered_object_is_silent():
    foo = Foo()
    remove_all(foo.bar)


# ---------------------------------------------------------------------------
# get_interceptors
# ---------------------------------------------------------------------------


def test_get_interceptors_returns_empty_for_unregistered():
    foo = Foo()
    assert get_interceptors(foo.bar) == []


def test_get_interceptors_returns_registered_entries():
    foo = Foo()
    def hook(): pass
    iid = register(foo.bar, hook, pass_self=True, callorder=-1)
    result = get_interceptors(foo.bar)
    assert len(result) == 1
    assert result[0]["id"] == iid
    assert result[0]["func"] is hook
    assert result[0]["pass_self"] is True
    assert result[0]["pass_args"] is False
    assert result[0]["pass_kwargs"] is False
    assert result[0]["is_context_manager"] is False
    assert result[0]["callorder"] == -1


def test_get_interceptors_reflects_all_registered():
    foo = Foo()
    id1 = register(foo.bar, lambda: None, callorder=-2)
    id2 = register(foo.bar, lambda: None, callorder=1)
    result = get_interceptors(foo.bar)
    assert [e["id"] for e in result] == [id1, id2]


def test_get_interceptors_empty_after_remove_all():
    foo = Foo()
    register(foo.bar, lambda: None, callorder=-1)
    remove_all(foo.bar)
    assert get_interceptors(foo.bar) == []


def test_get_interceptors_reflects_callable_callorder():
    foo = Foo()
    def order_fn(): return -1
    register(foo.bar, lambda: None, callorder=order_fn)
    result = get_interceptors(foo.bar)
    assert result[0]["callorder"] is order_fn


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_hooks_are_instance_specific():
    foo1 = Foo()
    foo2 = Foo()
    calls = []
    register(foo1.bar, lambda: calls.append("foo1"), callorder=-1)
    foo1.bar()
    assert calls == ["foo1"]
    foo2.bar()
    assert calls == ["foo1"]  # foo2 unaffected


def test_hooks_are_method_specific():
    foo = Foo()
    calls = []
    register(foo.bar, lambda: calls.append("bar_hook"), callorder=-1)
    foo.baz()
    assert calls == []  # baz unaffected


def test_restored_method_still_works_correctly():
    foo = Foo()
    iid = register(foo.bar, lambda: None, callorder=-1)
    remove(foo.bar, iid)
    assert foo.bar("x") == "bar(x)"
