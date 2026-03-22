from contextlib import contextmanager

import pytest

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
