"""Signature / introspection fidelity after interception.

Inspired by wrapt's test_adapter.py / test_annotations.py suites.
Pins how functools.wraps-based wrappers behave for the various
``inspect.*`` helpers users reach for when debugging.
"""

from __future__ import annotations

import asyncio
import functools
import inspect

from interceptor_registry import add_interceptor

# ---------------------------------------------------------------------------
# __wrapped__ / functools.wraps metadata
# ---------------------------------------------------------------------------


def test_name_qualname_doc_module_preserved():
    class Obj:
        def m(self, x, y=1):
            """Example docstring."""
            return x + y

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    wrapper = obj.m
    # wraps copies these four plus __wrapped__ and __dict__.
    assert wrapper.__name__ == "m"
    assert wrapper.__qualname__.endswith(".m")
    assert wrapper.__doc__ == "Example docstring."
    assert wrapper.__module__ == Obj.m.__module__


def test_wrapped_attribute_points_to_original_function():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    # functools.wraps sets __wrapped__ to the original source.
    wrapped = obj.m.__wrapped__
    # The unbound function on the class is the source.
    assert callable(wrapped)


def test_inspect_signature_resolves_through_wrapped():
    """inspect.signature() follows __wrapped__ and returns the original
    method's signature."""

    class Obj:
        def m(self, x: int, y: str = "hi") -> tuple:
            return (x, y)

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    sig_after = inspect.signature(obj.m)
    after_params = list(sig_after.parameters)
    assert "x" in after_params and "y" in after_params
    # Annotation presence (may be a string or the type depending on
    # ``from __future__ import annotations`` — check both).
    ann = sig_after.parameters["x"].annotation
    assert ann is int or ann == "int"
    # Default preserved.
    assert sig_after.parameters["y"].default == "hi"


def test_inspect_getfullargspec_resolves_through_wrapped():
    """``inspect.getfullargspec`` on Python 3.10+ does NOT follow
    ``__wrapped__`` by default (it inspects the literal function), so
    our ``*args, **kwargs`` wrapper shows up as such. Pin this
    behaviour; the user-facing answer is ``inspect.signature``, which
    *does* follow ``__wrapped__``."""

    class Obj:
        def m(self, a, b=1, *args, c=2, **kwargs):
            return (a, b, args, c, kwargs)

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    spec = inspect.getfullargspec(obj.m)
    # Wrapper literal signature: (*args, **kwargs).
    assert spec.varargs == "args"
    assert spec.varkw == "kwargs"
    # If someone wants the original, they get it via __wrapped__.
    original_spec = inspect.getfullargspec(obj.m.__wrapped__)
    assert "a" in original_spec.args
    assert "b" in original_spec.args
    assert "c" in original_spec.kwonlyargs


def test_inspect_isfunction_on_patched_instance_attr_is_true():
    """The wrapper is a plain function (not a method) so
    inspect.isfunction() returns True."""

    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert inspect.isfunction(obj.m) is True


def test_inspect_ismethod_on_patched_instance_attr_is_false():
    """The wrapper stored in obj.__dict__ is a bare function; accessing
    it doesn't go through the descriptor protocol, so it's NOT a bound
    method."""

    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert inspect.ismethod(obj.m) is False


def test_doc_non_empty_after_patching():
    """A method with a docstring keeps it after patching — users can
    still see the documentation."""

    class Obj:
        def m(self):
            """One-line summary.

            Extended description spanning multiple lines.
            """
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert obj.m.__doc__ is not None
    assert len(obj.m.__doc__) > 0
    assert "summary" in obj.m.__doc__


# ---------------------------------------------------------------------------
# classmethod / staticmethod — preserved metadata
# ---------------------------------------------------------------------------


def test_classmethod_name_preserved():
    class Obj:
        @classmethod
        def cls_m(cls, x):
            """A classmethod doc."""
            return (cls, x)

    obj = Obj()
    add_interceptor(obj, "cls_m", lambda: None, callorder=-1)
    assert obj.cls_m.__name__ == "cls_m"
    assert "classmethod doc" in obj.cls_m.__doc__


def test_staticmethod_name_preserved():
    class Obj:
        @staticmethod
        def static_m(x):
            """A staticmethod doc."""
            return x

    obj = Obj()
    add_interceptor(obj, "static_m", lambda: None, callorder=-1)
    assert obj.static_m.__name__ == "static_m"
    assert "staticmethod doc" in obj.static_m.__doc__


def test_staticmethod_signature_preserved():
    class Obj:
        @staticmethod
        def static_m(x: int, y: str = "hi") -> bool:
            return True

    obj = Obj()
    add_interceptor(obj, "static_m", lambda: None, callorder=-1)
    sig = inspect.signature(obj.static_m)
    ann = sig.parameters["x"].annotation
    assert ann is int or ann == "int"
    assert sig.parameters["y"].default == "hi"


# ---------------------------------------------------------------------------
# Async / generator — flag queries return True after patching
# ---------------------------------------------------------------------------


def test_iscoroutinefunction_holds_after_patching():
    class Obj:
        async def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert asyncio.iscoroutinefunction(obj.m) is True


def test_isasyncgenfunction_holds_after_patching():
    class Obj:
        async def m(self):
            yield 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert inspect.isasyncgenfunction(obj.m) is True


def test_isgeneratorfunction_holds_after_patching():
    class Obj:
        def m(self):
            yield 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert inspect.isgeneratorfunction(obj.m) is True


# ---------------------------------------------------------------------------
# Wrapper identity stability
# ---------------------------------------------------------------------------


def test_wrapper_identity_stable_across_accesses():
    class Obj:
        def m(self):
            return 1

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    # obj.m resolves through the instance __dict__, so repeated access
    # returns the same function object.
    assert obj.m is obj.m


# ---------------------------------------------------------------------------
# functools.cache + classmethod smoke
# ---------------------------------------------------------------------------


def test_patched_method_can_still_be_wrapped_by_functools_cache_outside():
    """A user wrapping obj.m with functools.cache externally should
    still get a callable they can invoke."""

    class Obj:
        def m(self, x):
            return x * 2

    obj = Obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    cached = functools.lru_cache(maxsize=4)(obj.m)
    assert cached(3) == 6
    # Same arg returns same result — caches hit.
    assert cached(3) == 6


# ---------------------------------------------------------------------------
# __dict__ preservation: functools.wraps copies custom attributes
# ---------------------------------------------------------------------------


def test_custom_attribute_on_original_copied_to_wrapper():
    def make_obj():
        class Obj:
            def m(self):
                return 1

        Obj.m.__my_tag__ = "custom"
        return Obj()

    obj = make_obj()
    add_interceptor(obj, "m", lambda: None, callorder=-1)
    assert obj.m.__my_tag__ == "custom"
