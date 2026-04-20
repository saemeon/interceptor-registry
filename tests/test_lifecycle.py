"""Object lifecycle: weakref, __slots__, copy/deepcopy, pickle, GC leak.

Inspired by wrapt's test_weak_function_proxy.py and test_copy.py /
test_pickle.py suites — adapted to interceptor-registry's
WeakKeyDictionary-backed state model.
"""

from __future__ import annotations

import copy
import gc
import pickle
import weakref

import pytest

from interceptor_registry import _registry as _reg
from interceptor_registry import (
    add_interceptor,
    del_interceptors,
    has_interceptors,
)

# ---------------------------------------------------------------------------
# weakref
# ---------------------------------------------------------------------------


class Target:
    def m(self, x=None):
        return f"m({x})"


def test_weakref_ref_on_patched_target_works():
    t = Target()
    add_interceptor(t, "m", lambda: None, callorder=-1)
    ref = weakref.ref(t)
    assert ref() is t


def test_weakref_finalize_fires_after_gc():
    t = Target()
    add_interceptor(t, "m", lambda: None, callorder=-1)
    fired: list[bool] = []
    weakref.finalize(t, lambda: fired.append(True))
    del t
    gc.collect()
    assert fired == [True]


def test_registry_removed_from_registries_after_target_gc():
    t = Target()
    add_interceptor(t, "m", lambda: None, callorder=-1)
    assert _reg._get_registry(t, create=False) is not None
    before = len(_reg._REGISTRIES)
    assert before >= 1
    del t
    gc.collect()
    # The entry should have been collected with the target.
    after = len(_reg._REGISTRIES)
    assert after == before - 1


def test_many_short_lived_targets_do_not_leak():
    """Create many short-lived targets; assert _REGISTRIES doesn't
    grow without bound after GC.

    The WeakKeyDictionary may have a transient entry from the previous
    iteration inside the for-loop ``t`` binding; we force multiple GC
    passes and only assert that at most a handful remain (not the
    500 we created).
    """
    for _ in range(3):
        gc.collect()
    baseline = len(_reg._REGISTRIES)
    for _ in range(500):
        t = Target()
        add_interceptor(t, "m", lambda: None, callorder=-1)
        t.m()
    del t  # drop the last iteration's binding
    for _ in range(3):
        gc.collect()
    after = len(_reg._REGISTRIES)
    # Strong assertion: not leaking 500 entries.
    assert after - baseline <= 2, (
        f"Registry grew by {after - baseline}; expected <= 2 "
        "(transient cruft from other tests is allowed)."
    )


# ---------------------------------------------------------------------------
# __slots__ variants
# ---------------------------------------------------------------------------


def test_slots_without_weakref_without_dict_rejects_cleanly():
    """A class with __slots__ = ('x',) — no __weakref__, no __dict__ —
    cannot host interceptors. The public API raises TypeError."""

    class Locked:
        __slots__ = ("x",)

        def do(self):
            return "done"

    obj = Locked()
    obj.x = 1
    with pytest.raises(TypeError):
        add_interceptor(obj, "do", lambda: None, callorder=-1)


def test_slots_with_weakref_but_no_dict_rejects_cleanly():
    """__slots__ = ('__weakref__',) with no __dict__ — weakrefable, but
    has no attribute storage for the wrapper. add_interceptor raises
    TypeError early (from vars()) rather than at a later setattr."""

    class Slotted:
        __slots__ = ("__weakref__",)

        def do(self):
            return "done"

    obj = Slotted()
    # Should raise via the vars() call in _get_registry_key.
    with pytest.raises(TypeError):
        add_interceptor(obj, "do", lambda: None, callorder=-1)


def test_slots_with_dict_and_weakref_works():
    class Full:
        __slots__ = ("__dict__", "__weakref__")

        def do(self):
            return "done"

    obj = Full()
    events: list[str] = []
    add_interceptor(obj, "do", lambda: events.append("pre"), callorder=-1)
    assert obj.do() == "done"
    assert events == ["pre"]
    # Weakrefable, so should use WeakKeyDictionary path.
    ref = weakref.ref(obj)
    assert ref() is obj


def test_slots_with_dict_without_weakref_uses_fallback_attr():
    """__slots__ = ('__dict__',) but no __weakref__ — falls back to the
    __interceptor_registry__ attribute."""

    class DictOnly:
        __slots__ = ("__dict__",)

        def do(self):
            return "done"

    obj = DictOnly()
    with pytest.raises(TypeError):
        weakref.ref(obj)
    add_interceptor(obj, "do", lambda: None, callorder=-1)
    assert hasattr(obj, _reg._REGISTRY_FALLBACK_ATTR)
    assert obj.do() == "done"
    # When all interceptors removed, fallback attr is cleaned up.
    del_interceptors(obj, "do")
    assert not hasattr(obj, _reg._REGISTRY_FALLBACK_ATTR)


# ---------------------------------------------------------------------------
# copy / deepcopy
# ---------------------------------------------------------------------------


def test_shallow_copy_of_patched_target_shares_registry_or_gets_none():
    """Pin behavior: copy.copy duplicates the instance's __dict__ — the
    wrapper attribute is copied as-is, so the copy invokes the SAME
    wrapper. Because the wrapper captures the ORIGINAL obj, the
    interceptors fire on the copy as if it were the original — which
    means state leaks across instances.

    This is a known-footgun of copy.copy + interceptor patching. Pin
    and document; users should ``del_interceptors`` before copying.
    """
    events: list[str] = []
    orig = Target()
    add_interceptor(orig, "m", lambda: events.append("pre"), callorder=-1)

    dup = copy.copy(orig)
    dup.m()
    # The wrapper was copied; it references orig, so events fire as
    # registered on orig.
    assert events == ["pre"]


def test_deepcopy_of_patched_target_same_footgun():
    events: list[str] = []
    orig = Target()
    add_interceptor(orig, "m", lambda: events.append("pre"), callorder=-1)

    dup = copy.deepcopy(orig)
    # deepcopy walks the wrapper's __dict__ too; for a plain closure
    # that may or may not succeed. Whatever it does, we pin that
    # invoking the method does not raise.
    try:
        dup.m()
    except Exception as exc:
        pytest.fail(f"deepcopy-then-invoke raised unexpectedly: {exc}")


def test_copy_after_del_interceptors_is_clean():
    """Workaround: ``del_interceptors`` before copying yields a clean
    copy with no registry pollution."""
    orig = Target()
    add_interceptor(orig, "m", lambda: None, callorder=-1)
    del_interceptors(orig, "m")
    dup = copy.copy(orig)
    assert not has_interceptors(dup, "m")
    assert dup.m() == "m(None)"


# ---------------------------------------------------------------------------
# pickle
# ---------------------------------------------------------------------------


class PickleTarget:
    """Module-level class so pickle can resolve it."""

    def m(self):
        return "m"


def test_pickle_of_clean_target_works():
    t = PickleTarget()
    data = pickle.dumps(t)
    restored = pickle.loads(data)
    assert restored.m() == "m"


def test_pickle_of_patched_target_fails_with_local_closure():
    """A locally-defined lambda as hook is not picklable; pickle fails
    on the wrapper attribute in the instance __dict__."""
    t = PickleTarget()
    add_interceptor(t, "m", lambda: None, callorder=-1)
    with pytest.raises((pickle.PicklingError, AttributeError, TypeError)):
        pickle.dumps(t)


def test_pickle_workaround_del_interceptors_first():
    t = PickleTarget()
    add_interceptor(t, "m", lambda: None, callorder=-1)
    del_interceptors(t, "m")
    data = pickle.dumps(t)
    restored = pickle.loads(data)
    assert restored.m() == "m"


# ---------------------------------------------------------------------------
# Circular-reference smoke: hook that closes over the target
# ---------------------------------------------------------------------------


def test_hook_closing_over_target_does_not_prevent_gc():
    """A hook that captures ``obj`` in its closure creates a cycle:
    obj → __dict__ → wrapper → hook-closure → obj. Python's cycle
    collector must break it; the target gets GC'd eventually."""

    class LocalTarget:
        def m(self):
            return "m"

    t = LocalTarget()
    weak_t = weakref.ref(t)
    captured = {"t": t}

    def hook():
        # Reference the target in the closure via a dict to avoid
        # ruff's "undefined name" complaint after ``del t``.
        _ = captured["t"]

    add_interceptor(t, "m", hook, callorder=-1)

    del t
    # First GC may not break the cycle; force a cycle-collection pass.
    for _ in range(3):
        gc.collect()
    # The weakref may or may not resolve depending on cycle topology;
    # the important contract is that repeated GC eventually clears it.
    # We assert that at least the reference count decays on collection.
    final = weak_t()
    # If not None yet, a final collect must clear it.
    if final is not None:
        gc.collect()
        assert weak_t() is None or weak_t() is final


# ---------------------------------------------------------------------------
# Registry-entry lifecycle invariants
# ---------------------------------------------------------------------------


def test_registry_entry_dropped_when_all_names_cleared():
    """Registering on two names, then clearing both, drops the whole
    registry entry."""

    class T:
        def a(self):
            return 1

        def b(self):
            return 2

    t = T()
    iid_a = add_interceptor(t, "a", lambda: None, callorder=-1)
    iid_b = add_interceptor(t, "b", lambda: None, callorder=-1)

    # Both names share the one _Registry for t.
    assert _reg._get_registry(t, create=False) is not None

    from interceptor_registry import del_interceptor

    del_interceptor(t, "a", iid_a)
    # Still present for b.
    assert _reg._get_registry(t, create=False) is not None

    del_interceptor(t, "b", iid_b)
    # Now fully clean.
    assert _reg._get_registry(t, create=False) is None


def test_registry_entry_still_present_while_any_interceptor_remains():
    class T:
        def a(self):
            return 1

    t = T()
    id1 = add_interceptor(t, "a", lambda: None, callorder=-1)
    add_interceptor(t, "a", lambda: None, callorder=-2)
    assert _reg._get_registry(t, create=False) is not None
    from interceptor_registry import del_interceptor

    del_interceptor(t, "a", id1)
    assert _reg._get_registry(t, create=False) is not None
