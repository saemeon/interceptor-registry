# interceptor-registry — v0.2 design record

**Status**: v0.2.0 **shipped.** 292 tests passing (288 unit + 4
doctests run via `--doctest-modules`), 100% coverage, ruff /
ruff format / ty / `mkdocs build --strict` all clean. Phases 1 and
2 of the plan below landed complete; the three bugs surfaced
during extensive testing were all fixed before release.

**Kept as a historical record** of how v0.2 was planned, what
design decisions were made, and which wrapt patterns were
adopted / rejected. For day-to-day contributing guidance see
[`CONTRIBUTING.md`](CONTRIBUTING.md), [`README.md`](README.md), and
the rendered docs at <https://saemeon.github.io/interceptor-registry/>.
Sections below that describe *how to execute* the plan (three-step
contract, phase ordering, final-summary template) are retained as
process documentation for future improvement cycles; they do not
apply to the v0.2 work, which is complete.

Seven improvements were identified after a careful read of
interceptor-registry's source and a comparison against
[wrapt](https://github.com/GrahamDumpleton/wrapt) (BSD-2, Graham
Dumpleton) — the reference Python library for object proxies and
decorator plumbing. Every item either (a) fixed a correctness gap
in interceptor-registry that wrapt handles correctly, or
(b) adopted a wrapt design pattern that cleaned up the existing
code. Specific wrapt files / line ranges are cited at each decision
point, and collected in the "References" section at the bottom.

## How this doc was used (process-documentation; does not apply to v0.2, which is done)

**A three-step contract for future improvement runs, not a straight "go implement" order.**
Skipping step 2 (the confirmation handshake) is not allowed.

### Step 1 — orient (no code yet)

1. **Read this file cover to cover before writing any code.**
2. Read the current source:
   [`src/interceptor_registry/_registry.py`](src/interceptor_registry/_registry.py)
   (~480 lines, single module — do not skim).
3. Read the existing tests:
   [`tests/test_registry.py`](tests/test_registry.py) (~500 lines)
   and [`tests/test_smoke.py`](tests/test_smoke.py).
4. Read [`README.md`](README.md) for the current public contract
   and [`pyproject.toml`](pyproject.toml) for the toolchain
   (`uv`, `ruff`, `ty`, `prek`, `setuptools-scm`).
5. (Recommended) Skim the wrapt files cited in References below —
   they're the design inspiration for items 1, 3, 4, 5, 6. A full
   wrapt source tree is checked out in a sibling directory of this
   repo: **`../dash-wrap/wrapt/`** (same workspace, pre-cloned for
   exactly this kind of reference lookup). Paths in References
   below point there directly. Priority reads:
   - [`../dash-wrap/wrapt/src/wrapt/proxies.py`](../dash-wrap/wrapt/src/wrapt/proxies.py)
     and
     [`../dash-wrap/wrapt/src/wrapt/wrappers.py`](../dash-wrap/wrapt/src/wrapt/wrappers.py)
     — how `ObjectProxy` handles `__await__`, `__aiter__`,
     `__iter__`, `__next__`; the `AutoObjectProxy` per-instance
     class-creation pattern is relevant to item 1.
   - [`../dash-wrap/wrapt/src/wrapt/synchronization.py`](../dash-wrap/wrapt/src/wrapt/synchronization.py)
     — the `@synchronized` decorator's per-object lock registry
     (async-aware, `WeakKeyDictionary`-backed). Inspiration for
     item 3.

### Step 2 — confirm with the user (REQUIRED before Phase 1)

Do not start coding after orientation. First produce a
**confirmation message** — a short (<300 words) write-up sent to
the user in chat — covering:

- **Your understanding of interceptor-registry in one paragraph**
  (what it does, how state is stored, why the instance-dict patching
  works for methods but not properties, what the public API
  surface is).
- **Which of the claims in this plan you verified yourself**. At
  minimum: try the failing async CM-hook test from Phase 1 item 1
  — add it, run it, and confirm the ordering is
  `["enter", "exit", "body"]` (the bug) and not
  `["enter", "body", "exit"]` (what we want). If the test actually
  passes, something has changed since this plan was written —
  stop and flag it. Also eyeball the property bug (add a test
  `add_interceptor(foo, 'some_property', ...)` and see the
  `TypeError` raised at call time).
- **Concerns or disagreements with the plan** before you start.
  Is the ordering (Phase 1 item 1 → 2 → 3) right? Are there items
  you think should be merged, split, or dropped? Any test approach
  you'd rather take? Any version-bump strategy you'd push back on?
- **What you plan to do first** — one sentence.

Then **wait for the user's reply**. Do not start Phase 1 until
they confirm or adjust. If the user says "go ahead" with no
changes, proceed. If they adjust the plan, incorporate the
adjustments before starting.

This confirmation step exists because the plan was written by a
different agent reading the code on a different day. Reality may
have drifted — the user switching computers, Python 3.14 changes,
a pre-existing PR sitting in a branch. Five minutes of
confirmation up front beats half a day of work pointed at the
wrong problem.

### Step 3 — execute

After user confirmation:

1. Execute **Phase 1** (correctness fixes).
2. Then **Phase 2** (testing matrix).
3. **Phase 3** is optional polish — only do it if the user
   explicitly asks in their confirmation reply or a later message.
4. After every phase: run the full test suite + linters. Do not
   proceed to the next phase with reds. See "Quality bar" at the
   bottom.
5. Do not edit `../dash-wrap/wrapt/` — it's a read-only reference
   clone, not part of this project. Reading it is encouraged;
   changes aren't.

## Ownership boundaries

- **You own**: all source, test, CI, config changes listed below.
- **You do not own**: creating GitHub releases, bumping the version
  tag, PyPI uploads. Stop at "ready for user to tag and release" and
  summarise what was built.
- **Version policy**: this plan bumps the public API behaviour in
  Phase 3 item 4 (storage refactor, minor breaking change). Phase 1
  and Phase 2 are purely backwards-compatible — safe for a patch
  release (`0.1.x`). Phase 3 warrants a minor bump (`0.2.0`). Flag
  this explicitly in the final summary so the user can decide.

## Context — what interceptor-registry is

A micro-library (~480 LoC, zero deps) that adds pre/post/around
interceptors on **bound methods of a single instance** at runtime,
without modifying the class.

Primary API:

```python
from interceptor_registry import (
    add_interceptor, del_interceptor, del_interceptors,
    has_interceptors, get_interceptors,
)

add_interceptor(foo, "method_name", hook_fn,
                pass_self=False, pass_args=False, pass_kwargs=False,
                is_context_manager=False, callorder=1)
# → returns int id
del_interceptor(foo, "method_name", iid)
del_interceptors(foo, "method_name")
has_interceptors(foo, "method_name") -> bool
get_interceptors(foo, "method_name") -> list[dict]
```

Mechanics today:

- `add_interceptor` looks up the raw descriptor via MRO
  (`_lookup_raw_descriptor`), gets the bound callable via
  `getattr(obj, name)`, builds a `functools.wraps`-ed closure
  (`_make_wrapper`), and sets it as `obj.__dict__[name]` so it
  shadows the class-level method for that single instance.
- State lives directly on the user's instance:
  `_registered_interceptors`, `_registered_interceptors_id_gen`,
  `_registered_interceptors_originals`.
- The wrapper closure dispatches through `_call_method_with_hooks`
  which sorts hooks by `callorder` (negative pre, positive post),
  runs them around a synchronous `method(*args, **kwargs)` call,
  using `ExitStack` for context-manager hooks.

Works today for: instance methods, classmethods, staticmethods. Does
not work for: `async def`, generators, properties, concurrent
modifications.

## The seven improvements

Ranked by impact and priority. Work through them in the order below.

### Phase 1 — correctness fixes (must-have, v0.1.x compatible)

#### 1. Async and generator methods (real correctness bug)

**Problem**. `_call_method_with_hooks` is fully synchronous. For
`async def` methods:

```python
result = method(*args, **kwargs)   # returns a coroutine, does NOT await
```

Post-hooks run before the coroutine body executes; context-manager
hooks' `__exit__` fires before the real work. The caller then
`await`s the returned coroutine, which runs the method body in
isolation.

Same shape of bug applies to:

- plain generator methods (`def foo(self): yield ...`) —
  `method(...)` returns a generator without running the body,
- async generator methods (`async def foo(self): yield ...`) —
  same but async.

**Failing test** (add to `tests/test_registry.py`; use as a red
before writing the fix):

```python
import asyncio
from contextlib import contextmanager
import pytest


@pytest.mark.asyncio
async def test_async_method_cm_hook_wraps_body_not_coroutine():
    """is_context_manager=True on an async method must wrap the
    actual awaited body, not the coroutine return."""
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
    # Must be: pre, enter, body, post, exit — NOT pre, enter, post, exit, body.
    assert events == ["pre", "enter", "body", "post", "exit"]


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


@pytest.mark.asyncio
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
```

**Add `pytest-asyncio`** to the `test` dependency group in
`pyproject.toml`:

```toml
test = ["pytest", "pytest-cov", "pytest-asyncio"]
```

And configure asyncio mode in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
# ... existing options ...
asyncio_mode = "auto"
```

**Implementation approach**. Add detection in `_make_wrapper` and
route to one of four wrapper bodies:

```python
import asyncio
import inspect


def _make_wrapper(obj, original_callable, registry_key):
    source = getattr(original_callable, "__func__", original_callable)

    if asyncio.iscoroutinefunction(source):
        @functools.wraps(source)
        async def wrapper(*args, **kwargs):
            return await _call_method_with_hooks_async(
                obj, original_callable, registry_key, *args, **kwargs
            )
    elif inspect.isasyncgenfunction(source):
        @functools.wraps(source)
        async def wrapper(*args, **kwargs):
            async for v in _call_method_with_hooks_async_gen(
                obj, original_callable, registry_key, *args, **kwargs
            ):
                yield v
    elif inspect.isgeneratorfunction(source):
        @functools.wraps(source)
        def wrapper(*args, **kwargs):
            yield from _call_method_with_hooks_gen(
                obj, original_callable, registry_key, *args, **kwargs
            )
    else:
        @functools.wraps(source)
        def wrapper(*args, **kwargs):
            return _call_method_with_hooks(
                obj, original_callable, registry_key, *args, **kwargs
            )

    setattr(wrapper, _REGISTRY_KEY_ATTR, registry_key)
    setattr(wrapper, _INTERCEPTOR_OWNER_ATTR, obj)
    return wrapper
```

Add parallel dispatch helpers that share a single `_prepare_hooks`
helper for the sorting / callorder-resolution / validation
(currently inlined at the top of `_call_method_with_hooks`). Extract
that logic so you don't duplicate it four times. Sketch:

```python
def _prepare_hooks(obj, method, registry_key):
    """Resolve callorders and sort. Raises ValueError on order==0.

    Returns a list of (func, pass_self, pass_args, pass_kwargs, is_cm, order)
    tuples sorted by ascending order.
    """
    all_hooks = list(obj._registered_interceptors[registry_key].items())
    processed = []
    for iid, (func, pass_self, pass_args, pass_kwargs, is_cm, callorder) in all_hooks:
        resolved = _call_if_is_callable(callorder)
        if resolved == 0:
            raise ValueError(
                f"Interceptor {iid!r} for "
                f"'{getattr(method, '__name__', method)}' "
                "resolved callorder=0. Use a negative value for pre-hooks "
                "or a positive value for post-hooks."
            )
        processed.append((func, pass_self, pass_args, pass_kwargs, is_cm, resolved))
    return sorted(processed, key=lambda x: x[-1])


def _call_method_with_hooks(obj, method, registry_key, *args, **kwargs):
    sorted_hooks = _prepare_hooks(obj, method, registry_key)
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        result = method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)
    return result


async def _call_method_with_hooks_async(obj, method, registry_key, *args, **kwargs):
    sorted_hooks = _prepare_hooks(obj, method, registry_key)
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        result = await method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)
    return result


def _call_method_with_hooks_gen(obj, method, registry_key, *args, **kwargs):
    sorted_hooks = _prepare_hooks(obj, method, registry_key)
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        yield from method(*args, **kwargs)
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)


async def _call_method_with_hooks_async_gen(obj, method, registry_key, *args, **kwargs):
    sorted_hooks = _prepare_hooks(obj, method, registry_key)
    with ExitStack() as stack:
        _run_pre_hooks(obj, sorted_hooks, stack, args, kwargs)
        async for v in method(*args, **kwargs):
            yield v
        _run_post_hooks(obj, sorted_hooks, stack, args, kwargs)
```

**Subtle points**:

- For `async def` methods, the pre/post hooks themselves stay
  synchronous. Async pre/post hooks are out of scope for v0.2 — if
  a user passes an `async def` as a hook, today's library returns
  the coroutine from `_trigger_hook` without awaiting. **Keep that
  behaviour** for v0.2 and document it in the docstring as a known
  limitation. Async hooks are a separate feature request.
- If a user passes `is_context_manager=True` with an async context
  manager (`@asynccontextmanager`), `stack.enter_context(rv)` will
  raise because `rv.__enter__` doesn't exist. Document that async
  CMs are not supported in v0.2 (would need `AsyncExitStack` inside
  the async wrappers). For now, raise a clear error:

  ```python
  if is_cm and not hasattr(rv, "__enter__"):
      raise TypeError(
          f"Interceptor {hook_func!r} returned {type(rv).__name__}, "
          "which is not a synchronous context manager. Async context "
          "managers are not supported in this version."
      )
  ```

- The existing `_call_method_with_hooks` signature shouldn't change
  (internal but used in tests if you grep). If you extract helpers,
  keep the name as a thin wrapper.

**Tests to add** (in addition to the three red-tests above):

- Sync method path still works (regression — all existing tests).
- Async method with pre-only, post-only, both.
- Async method with exception raised — pre-hook CM's `__exit__`
  receives the exception.
- Async method with `pass_self` / `pass_args` / `pass_kwargs`.
- Sync generator yielding N values — post-hook fires once after
  full iteration.
- Sync generator with early break — CM still exits (ExitStack
  guarantees). Assert behaviour explicitly.
- Async generator — parallel to sync generator.
- Async method whose coroutine is garbage-collected unwaited —
  ensure no lingering state. (Use `asyncio.get_event_loop().close()`
  or garbage-collect manually and check that the registry survives.)

**Acceptance**: all three red-tests go green; no existing test
regresses; `asyncio.iscoroutinefunction(foo.do) is True` after
patching (the wrapper is correctly async).

#### 2. Explicit property rejection

**Problem**. Today:

```python
raw = _lookup_raw_descriptor(type(obj), name)        # returns property object
original_callable = getattr(obj, name)               # INVOKES the getter,
                                                      # returns its return value
wrapper = _make_wrapper(obj, original_callable, ...) # closure captures a string
```

Calling the patched property raises `TypeError: 'str' object is not
callable` (or similar) — a hostile failure that makes users debug
the wrong thing.

The README scope is "instance methods, classmethods, and static
methods". Properties are documented out-of-scope. Fix: detect and
reject with a clear message.

**Implementation** (in `add_interceptor`, immediately after the
`_lookup_raw_descriptor` call):

```python
raw = _lookup_raw_descriptor(type(obj_any), name)

if isinstance(raw, property):
    raise TypeError(
        f"Cannot intercept property '{name}' on {type(obj_any).__name__}. "
        "interceptor-registry supports instance methods, classmethods, "
        "and staticmethods. To intercept a property's getter or setter, "
        "wrap the underlying function and rebuild the property on the "
        "class."
    )
```

Consider extending the check to other non-callable descriptors:

```python
if not callable(raw) and not isinstance(raw, (staticmethod, classmethod)):
    raise TypeError(
        f"Cannot intercept '{name}' on {type(obj_any).__name__}: "
        f"{type(raw).__name__} is not a supported descriptor kind. "
        "Supported: instance method, classmethod, staticmethod."
    )
```

Use your judgment — the narrow `isinstance(raw, property)` check is
enough to cover the reported failure mode; the broader check
prevents surprise on future descriptor types. **Recommend both.**

**Tests to add**:

```python
def test_add_interceptor_on_property_raises():
    class Foo:
        @property
        def value(self):
            return 42

    with pytest.raises(TypeError, match="property"):
        add_interceptor(Foo(), "value", lambda: None, callorder=-1)


def test_add_interceptor_on_non_callable_class_attr_raises():
    class Foo:
        constant = "hello"

    with pytest.raises(TypeError, match="not.*supported"):
        add_interceptor(Foo(), "constant", lambda: None, callorder=-1)
```

**Acceptance**: clear TypeError at registration time instead of
confusing failure at invocation time.

#### 3. Thread safety

**Problem**. `add_interceptor` has a read-check-modify sequence on
lines ~291–298 of `_registry.py`:

```python
if registry_key not in obj_any._registered_interceptors:
    original_callable = getattr(obj_any, name)         # race starts here
    obj_any._registered_interceptors_originals[...] = ...
    wrapper = _make_wrapper(...)
    setattr(obj_any, name, wrapper)                    # race ends here
```

Two threads calling `add_interceptor(foo, 'bar', ...)` concurrently:

- Both see `registry_key not in obj._registered_interceptors` →
  both enter the branch.
- Thread A: `getattr(obj, 'bar')` → original method. Thread A sets
  `foo.bar = wrapper_A`.
- Thread B: `getattr(obj, 'bar')` → now returns `wrapper_A` (already
  installed). Thread B captures `wrapper_A` as its "original",
  builds `wrapper_B` that wraps `wrapper_A`. Sets `foo.bar =
  wrapper_B`. Result: double-wrapped method, broken restoration on
  `del_interceptor`.

Same race on `del_interceptor` / `del_interceptors`: two threads
clearing concurrently can double-`pop` from the registry, raising
`KeyError`.

**Implementation**. Minimal version using `WeakKeyDictionary` of
per-object locks:

```python
import threading
import weakref

# Module-level
_LOCKS: "weakref.WeakKeyDictionary[Any, threading.RLock]" = (
    weakref.WeakKeyDictionary()
)
_LOCKS_REGISTRY_LOCK = threading.Lock()


def _lock_for(obj) -> threading.RLock:
    """Return a per-object RLock, creating it on first access.

    Uses a WeakKeyDictionary so the lock dies with the object — no
    leak. Non-weakrefable objects (rare — most Python objects
    support weakrefs; objects with __slots__ excluding __weakref__
    do not) fall back to a module-level lock to preserve
    correctness.
    """
    try:
        existing = _LOCKS.get(obj)
        if existing is not None:
            return existing
    except TypeError:
        # obj not weakrefable — use module-level lock.
        return _LOCKS_REGISTRY_LOCK
    with _LOCKS_REGISTRY_LOCK:
        lock = _LOCKS.setdefault(obj, threading.RLock())
    return lock
```

Then wrap the public functions:

```python
def add_interceptor(obj, name, func, ...):
    # ... argument validation (non-locking) ...
    with _lock_for(obj):
        # ... existing body ...


def del_interceptor(obj, name, interceptor_id):
    with _lock_for(obj):
        # ... existing body ...


def del_interceptors(obj, name):
    with _lock_for(obj):
        # ... existing body ...
```

Use an **`RLock`**, not `Lock`: `_restore_original_method` calls
back into methods that may re-acquire; reentrant lock prevents
self-deadlock.

`has_interceptors` and `get_interceptors` are read-only — you can
skip locking them. But hoist the three attribute accesses into
local variables so you don't race with a concurrent mutation:

```python
def get_interceptors(obj, name):
    registry_key = _get_registry_key(obj, name)
    if registry_key is None or not hasattr(obj, "_registered_interceptors"):
        return []
    # Snapshot to avoid "dict changed size during iteration" races.
    with _lock_for(obj):
        entries = list(obj._registered_interceptors.get(registry_key, {}).items())
    return [
        {"id": iid, "func": func, ...}
        for iid, (func, ...) in entries
    ]
```

**Tests to add**:

```python
import concurrent.futures


def test_concurrent_add_interceptor_is_safe():
    foo = Foo()
    calls: list[str] = []

    def worker(i):
        return add_interceptor(
            foo, "bar", lambda: calls.append(f"hook{i}"), callorder=-(i + 1)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(worker, range(32)))

    assert len(set(ids)) == 32, "ids must be unique"
    foo.bar()
    assert len(calls) == 32, "every hook must fire exactly once"
    # The bar attribute must be wrapped exactly once, not N times.
    assert "bar" in vars(foo)


def test_concurrent_add_and_del_is_safe():
    foo = Foo()
    iids = [
        add_interceptor(foo, "bar", lambda: None, callorder=-(i + 1))
        for i in range(16)
    ]

    def remove(iid):
        del_interceptor(foo, "bar", iid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(remove, iids))

    assert not has_interceptors(foo, "bar")
    assert "bar" not in vars(foo)
```

**Note**: thread-safety tests are inherently flaky — races manifest
intermittently. Run them several times (`pytest --count=20`) during
development to build confidence. Consider adding a
`@pytest.mark.flaky(reruns=3)` or `pytest-repeat` guard — or mark
them `@pytest.mark.slow` and skip by default in CI. Document the
decision in the test docstring.

**Acceptance**: concurrent smoke tests pass 20/20 runs; no regression
in single-threaded tests; lock overhead negligible (measure with a
quick `timeit` on 1e5 `add/del` pairs before/after — should be
within 2×).

### Phase 2 — testing matrix (high leverage, v0.1.x compatible)

#### 7. Parametrise tests across method kinds and Python versions

Currently `tests/test_registry.py` only tests a single sync `Foo`
fixture. Extend coverage so every hook test runs against every
method kind the library supports.

**Test-fixture pattern** (add to `tests/test_registry.py`):

```python
import pytest


# Test matrix: one fixture per method kind. Each fixture returns
# (instance, method_name, invoker) where invoker is a zero-arg
# callable that triggers the method the way a real user would.

@pytest.fixture
def sync_method_target():
    events: list[str] = []

    class Target:
        def m(self):
            events.append("body")
            return 42

    return Target(), "m", lambda t: t.m(), events


@pytest.fixture
def classmethod_target():
    events: list[str] = []

    class Target:
        @classmethod
        def m(cls):
            events.append("body")
            return 42

    return Target(), "m", lambda t: t.m(), events


@pytest.fixture
def staticmethod_target():
    events: list[str] = []

    class Target:
        @staticmethod
        def m():
            events.append("body")
            return 42

    return Target(), "m", lambda t: t.m(), events


@pytest.fixture
async def async_method_target():
    events: list[str] = []

    class Target:
        async def m(self):
            events.append("body")
            return 42

    return Target(), "m", lambda t: asyncio.run(t.m()), events


# ... generator_target, async_gen_target similarly ...


@pytest.mark.parametrize(
    "target_fixture",
    ["sync_method_target", "classmethod_target", "staticmethod_target"],
)
def test_pre_hook_fires_across_method_kinds(request, target_fixture):
    target, name, invoke, events = request.getfixturevalue(target_fixture)
    add_interceptor(target, name, lambda: events.append("pre"), callorder=-1)
    result = invoke(target)
    assert result == 42
    assert events == ["pre", "body"]
```

Do the same for: post hook, pre+post, CM-around, argument
forwarding (`pass_self`, `pass_args`, `pass_kwargs`), exception
propagation.

**CI matrix**. Add `.github/workflows/test.yml` mirroring
dash-wrap's:

```yaml
name: Test
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --group test
      - run: uv run pytest
```

Add `.github/workflows/lint.yml`:

```yaml
name: Checks
on: [push, pull_request]

jobs:
  prek:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - run: uv sync --group dev
      - uses: j178/prek-action@v2
```

And `.github/workflows/publish.yml` mirroring dash-wrap's for PyPI
OIDC, if the project is slated for PyPI release.

**Acceptance**: every hook behaviour test runs at least once per
method kind; CI green on Python 3.10–3.14.

### Phase 3 — optional polish (confirm with user before starting)

These items are design cleanups and feature expansions. Skip them
unless the user explicitly confirms. If you do them, bump to
`0.2.0`.

#### 4. Wrapper-local state via `WeakKeyDictionary` (minor breaking change)

**Problem**. Today the registry lives on the user's instance:

- `foo._registered_interceptors`
- `foo._registered_interceptors_id_gen`
- `foo._registered_interceptors_originals`

Three attributes pollute `vars(foo)` / `dir(foo)`. Users debugging
via `print(vars(foo))` see implementation details. Minor, but
unclean.

**Implementation**. Store per-object state in a module-level
`WeakKeyDictionary[obj, _Registry]` where `_Registry` is an
internal dataclass:

```python
import itertools
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class _Registry:
    """Per-target registry state. Replaces the three ``_registered_*``
    instance attributes."""
    interceptors: dict[int, dict[int, tuple]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    id_gen: Iterator[int] = field(default_factory=itertools.count)
    originals: dict[int, tuple] = field(default_factory=dict)


_REGISTRIES: "weakref.WeakKeyDictionary[Any, _Registry]" = (
    weakref.WeakKeyDictionary()
)


def _get_registry(obj, create: bool = True) -> _Registry | None:
    try:
        existing = _REGISTRIES.get(obj)
    except TypeError:
        # Non-weakrefable object — fall back to instance-dict storage
        # with a clearly-namespaced private attribute. Rare; document
        # the limitation in the README.
        if not hasattr(obj, "__interceptor_registry__"):
            if not create:
                return None
            obj.__interceptor_registry__ = _Registry()
        return obj.__interceptor_registry__
    if existing is None and create:
        _REGISTRIES[obj] = _Registry()
        return _REGISTRIES[obj]
    return existing
```

Refactor every access to the old attributes to go through
`_get_registry`. Update:

- `_ensure_registry(obj)` → `_get_registry(obj, create=True)`
- `obj._registered_interceptors[key]` →
  `_get_registry(obj).interceptors[key]`
- `obj._registered_interceptors_originals[key]` →
  `_get_registry(obj).originals[key]`
- `obj._registered_interceptors_id_gen` →
  `_get_registry(obj).id_gen`
- `hasattr(obj, "_registered_interceptors")` →
  `_get_registry(obj, create=False) is not None`
- `_restore_original_method` — simplest: after the last interceptor
  is removed, call `_REGISTRIES.pop(obj, None)` to fully clean up.

**Breaking change note**. Anyone introspecting the three
`_registered_*` attributes directly (not part of the public API
but was observable) will see they're gone. Include in the release
notes. Bump to 0.2.0.

**Tests to add**:

- `_get_registry` returns same instance on repeated access for the
  same obj.
- Registry is garbage-collected when the target is — assert
  `_REGISTRIES` has 0 entries after `del foo; gc.collect()`.
- Non-weakrefable objects: `__slots__` without `__weakref__` falls
  back to attribute storage and still works (construct a tiny class
  with `__slots__ = ("x",)` to exercise the branch).

**Acceptance**: `vars(foo)` contains only `bar` (the wrapper) after
`add_interceptor(foo, "bar", ...)` — no `_registered_*` attrs
leak. Lifecycle tests pass.

#### 5. Full descriptor support (property getters / setters / deleters)

**Scope**. Extend interception beyond the three supported
descriptor kinds to properties. Optional feature; only do it if the
user explicitly requests it. If yes, follow wrapt's
`decorators.py` / `signature.py` patterns for descriptor-aware
binding.

**Sketch** (rough — flesh out before starting):

- Detect `isinstance(raw, property)`.
- Build a new property whose `fget` / `fset` / `fdel` are
  interceptor-aware closures, each running `_prepare_hooks` and
  dispatching to the original.
- Set the new property on `type(obj)` — NOT on the instance — so
  Python's descriptor protocol finds it (properties don't work via
  instance `__dict__`). This is a **class-level change** that
  affects all instances, which violates the "scoped to obj" design
  principle.
  - Workaround: create a dynamic subclass of `type(obj)`, install
    the interceptor-aware property on the subclass, and rebind
    `obj.__class__` to the subclass. Similar to how
    `matplotlib.axes._make_axes_gridspec` patches individual
    instances.
  - This is subtle enough that it warrants a design doc of its own
    before implementation. **Strongly consider deferring to v0.3.**

**Tests to add** (if implemented):

- Read-only property (only `fget`) — interceptor runs on read.
- Read-write property — interceptors run independently on
  `obj.x` (read) and `obj.x = ...` (write).
- Deleter — interceptor runs on `del obj.x`.
- Class identity: `type(obj)` changed; `isinstance(obj, OriginalCls)`
  still True (subclass relationship).
- Other instances of the same original class are unaffected.
- `del_interceptor` restores `obj.__class__` to the original class
  when the last interceptor is removed.

**If you do this**: this is a substantial change. Budget 2–3× the
LoC of #4. Consider writing a micro-design-doc first and getting
user sign-off before starting.

#### 6. Signature introspection fidelity beyond `functools.wraps`

**Problem**. `functools.wraps` copies `__name__`, `__qualname__`,
`__doc__`, `__module__`, `__wrapped__`, `__dict__`. Enough for:

- `inspect.signature(patched_method)` → resolves via `__wrapped__`.

Not enough for:

- `inspect.getsource(patched_method)` → returns the wrapper's
  source, not the original's.
- `inspect.getfullargspec(patched_method)` → may return the
  wrapper's `*args, **kwargs`, not the original's.
- Debugger "step into" lands in the wrapper.

wrapt's solution
([`wrapt/src/wrapt/signature.py`](../dash-wrap/wrapt/src/wrapt/signature.py),
[`wrapt/src/wrapt/decorators.py`](../dash-wrap/wrapt/src/wrapt/decorators.py)):
`_AdapterFunctionCode` / `_AdapterFunctionSurrogate` classes that
proxy `__code__`, `__defaults__`, `__kwdefaults__`, `__signature__`
to give `inspect.*` the correct answers.

**Implementation**. Port the relevant wrapt code into
`_registry.py` as private helpers (with attribution in a comment —
wrapt is BSD-2, MIT-compatible, but retain the copyright notice).
Construct an `_AdapterFunctionSurrogate` around the wrapper closure
before returning from `_make_wrapper`.

**Cost**. Moderate (~100 LoC; mostly copy-adapt from wrapt). Very
low impact for v0.2 — users don't typically `inspect.getsource` on
patched methods. Skip unless the user asks.

## Non-goals

Explicit things **not** in scope for this plan, even if a fresh
agent would otherwise be tempted:

- **Async hooks** (`async def` interceptor functions). The wrapper
  supports async *methods*; hooks stay synchronous. Document this
  as a known limitation in the public API docstrings. Async hooks
  require `AsyncExitStack` and are a v0.3 feature.
- **Async context-manager hooks**
  (`@asynccontextmanager` returning an async CM to `is_context_manager=True`).
  Same reason. Raise a clear `TypeError` when detected.
- **Decorator-style API** (`@interceptor(obj, 'bar', ...)`) — the
  current factory style is fine; no decorator wrapper.
- **Class-level interception** ("intercept `Foo.bar` for every
  instance") — out of scope by design. interceptor-registry is
  instance-scoped on purpose.
- **Pickling of patched objects** — users who need pickle should
  `del_interceptors` before dumping. Adding pickle support would
  require re-adding interceptors after load, changing the registry
  model.
- **C extension for performance** — at ~1 µs overhead per hook call
  already, Python is fast enough.
- **Removing the `wrapt` clone folder** if it exists in the
  workspace — that's a reference only, not part of this task.

## Quality bar (non-negotiable)

Before marking any phase done:

- `uv run pytest` green (including the new async / concurrent /
  matrix tests).
- `uv run ruff check` clean.
- `uv run ruff format --check` clean.
- `uv run ty check` clean.
- Coverage on `src/interceptor_registry/_registry.py` at 100% line
  coverage. Current tests already get close; new tests should
  maintain this.
- Every new public or private function has a numpy-style docstring
  (Parameters / Returns / Raises / Examples sections as
  appropriate). Match the style of the existing file.
- No `TODO` / `FIXME` / placeholder comments in shipped code.
- `README.md` updated to list the new supported method kinds
  (async, generator, async generator) in the one-liner "Works for
  …" sentence.
- `CHANGELOG.md`: one entry per phase, each listing the functional
  change from the user's perspective (not the implementation
  detail).

## Ordering / dependencies

Strict ordering:

1. **Phase 1 item 1** (async/gen) — biggest change; land first so
   everything else builds on a working dispatch path.
2. **Phase 1 item 2** (property rejection) — tiny, independent.
3. **Phase 1 item 3** (thread safety) — touches public API
   entry-points; land after #1 so the lock wraps the full dispatch.
4. **Phase 2 item 7** (matrix tests + CI) — reuses the new method
   kinds from #1.
5. **Phase 3** — only with user confirmation.

After each phase: commit with a descriptive message, run full
`uv run pytest` + `uv run ruff check` + `uv run ty check`, tag
progress in `CHANGELOG.md`.

## Final summary (write when done)

Produce a final message covering:

- Which phases were completed.
- Test counts: before / after, per phase (e.g., "Phase 1 added
  45 tests, suite went 38 → 83 passing").
- Version-bump recommendation: `0.1.x` for Phase 1+2, `0.2.0` if
  Phase 3 item 4 landed, `0.3.0` if Phase 3 item 5 landed.
- Any deviations from this plan and why (logged as "Questions from
  implementation" entries in this file, same structure dash-wrap
  uses).
- What the user needs to do next: review, tag, release.

## References

- Current source: [src/interceptor_registry/_registry.py](src/interceptor_registry/_registry.py)
- Current tests: [tests/test_registry.py](tests/test_registry.py)
- dash-wrap's CI layout for reference: `../dash-wrap/.github/workflows/`
- wrapt's async-aware proxy (if needed for #1 subtleties):
  `../dash-wrap/wrapt/src/wrapt/proxies.py` (AutoObjectProxy) and
  `../dash-wrap/wrapt/src/wrapt/wrappers.py` (ObjectProxy base)
- wrapt's signature proxying (for Phase 3 #6):
  `../dash-wrap/wrapt/src/wrapt/signature.py`
- wrapt's synchronization patterns (for Phase 1 #3 inspiration):
  `../dash-wrap/wrapt/src/wrapt/synchronization.py`
- wrapt's descriptor / binding handling (for Phase 3 #5):
  `../dash-wrap/wrapt/src/wrapt/decorators.py`

**Where the wrapt source lives.** The paths above resolve against
`../dash-wrap/wrapt/` — a full clone of
<https://github.com/GrahamDumpleton/wrapt> that Simon keeps in the
workspace for exactly this kind of cross-reference lookup. The
clone is gitignored inside `dash-wrap/` (we don't ship wrapt's
source with dash-wrap's PyPI package), but the directory **is
present on disk** in the local workspace at the time this plan was
written. If you arrive later and the directory has been cleaned
up: `pip install wrapt` in a scratch venv and read the installed
source under `.venv/lib/pythonX.Y/site-packages/wrapt/`, or
reclone upstream.

## Questions from implementation

### Async-hook rejection: at registration AND at call time

**Where**: `src/interceptor_registry/_registry.py:282` (in
`_trigger_hook`) and `src/interceptor_registry/_registry.py:645`
(in `add_interceptor`).

**Ambiguity**: the plan specifies rejecting async hooks with a
clear `TypeError`, but does not say whether the check should fire
at registration time (cheaper feedback, but only catches the
direct-registration path) or at call time (catches every path,
but delays the error).

**Decision**: check in both places. `add_interceptor` raises
eagerly so the common path fails fast with the file-and-line of
the bad registration.  `_trigger_hook` re-checks defensively so
that registry tampering (a test installs a tuple with an async
callable directly into `_Registry.interceptors`) still produces
the same `TypeError` at the call site rather than an unawaited-
coroutine warning much later.

**Rationale**: the belt-and-braces cost is two `if` branches and
one extra `TypeError` string — negligible — and the test suite
exercises both branches, so the defensive check is covered and
intentional.  Removing the registration-time check would move the
error from "bad add_interceptor call" to "next time the method is
invoked," which is a strictly worse UX.

**Cost to switch later**: trivial.  Delete either branch and the
other still catches the case.

### Non-weakrefable targets: fallback-attribute storage

**Where**: `src/interceptor_registry/_registry.py:97-114` and the
`_REGISTRY_FALLBACK_ATTR = "__interceptor_registry__"` constant on
line 23.

**Ambiguity**: the plan sketched a fallback to an
`__interceptor_registry__` instance attribute for objects whose
`WeakKeyDictionary.get` raises `TypeError` (typically classes with
`__slots__` that exclude `__weakref__`).  It did not specify what
should happen for targets that are *both* not weakrefable *and*
do not allow attribute assignment (e.g., `__slots__ = ("x",)` —
no `__weakref__`, no `__dict__`).

**Decision**: fall back to the attribute when the object allows
it; raise `TypeError("...object is not weak-referenceable and
does not allow attribute assignment")` otherwise.  In practice
this branch is not reachable through the public API because
`_get_registry_key` calls `vars(obj)` earlier, which raises its
own `TypeError` first on no-`__dict__` objects.  The branch is
therefore a defensive safety net that fires only when callers
drive `_get_registry` directly (e.g. future internal refactors or
test probes).

**Rationale**: a clear `TypeError` with a specific message is
strictly better than whatever the default `setattr` failure would
surface (it varies by Python version and slot layout).  Keeping
the branch costs six lines and is covered by a direct-helper
test.

**Cost to switch later**: trivial.  If a future version of the
library normalizes the early `vars(obj)` call (e.g., replaces it
with a safer probe), the fallback path becomes reachable through
the public API without further changes.

### Read-only introspection is lock-free

**Where**: `src/interceptor_registry/_registry.py:817`
(`has_interceptors`) and `:860` (`get_interceptors`).

**Ambiguity**: the plan hinted that read-only APIs *could* skip
the per-object lock but also said "hoist the three attribute
accesses into local variables so you don't race with a concurrent
mutation."  Either choice is defensible.

**Decision**: both functions are fully lock-free.  `has_interceptors`
reads a single `dict.get` result and returns `bool(...)` — a torn
read is impossible in CPython for a single `get`.
`get_interceptors` snapshots `bucket.items()` with `list(...)`
before iterating, which is atomic against a concurrent mutation
(the only risk is "dict changed size during iteration," which the
snapshot eliminates).

**Rationale**: lock acquisition on every `has_interceptors` call
is pure overhead for what is typically a hot introspection path
(users check before deciding whether to register).  The existing
test suite covers concurrent read-during-write scenarios and
passes.

**Cost to switch later**: trivial.  Wrapping both bodies in
`with registry.lock:` is a two-line change and does not break
the public contract (stale reads are already documented as
acceptable).
