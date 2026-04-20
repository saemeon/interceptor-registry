# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added

- Support for intercepting `async def` methods; pre-/post-hooks and
  `is_context_manager=True` hooks now correctly wrap the awaited
  body, not the coroutine-construction.
- Support for intercepting generator (`def foo(): yield ...`) and
  async-generator (`async def foo(): yield ...`) methods.
- Parametrised test matrix covering every supported method kind.
- GitHub Actions workflows: `test.yml` (Python 3.10-3.14 matrix),
  `lint.yml` (prek + ty), `publish.yml` (PyPI OIDC), `docs.yml`.
- Per-object `RLock` protecting concurrent `add_interceptor` /
  `del_interceptor` / `del_interceptors`.

### Fixed

- `is_context_manager=True` hooks on `async def` methods no longer
  exit before the method body runs (previously fired in the wrong
  order: `enter -> post-hooks -> exit -> body`).
- `add_interceptor` on a `@property` no longer silently fails or
  raises a confusing `AttributeError`; it now raises a clear
  `TypeError` at registration.
- Concurrent `add_interceptor` on the same target no longer
  double-wraps or captures a wrapper as the "original".
- `is_context_manager=True` hooks that suppress an exception raised
  from the method body no longer raise `UnboundLocalError`; the
  intercepted call now returns `None` when the exception is
  suppressed (consistent across sync, async, generator, and
  async-generator methods).
- Generator methods decorated with interceptors now preserve the
  `return <value>` terminator — the value is surfaced on the
  wrapper's `StopIteration.value`, so callers using
  `result = yield from obj.m()` observe the underlying return value
  unchanged.
- Async-generator methods now correctly forward `asend()`,
  `athrow()`, and `aclose()` to the underlying body through the
  interceptor layer. Previously `asend` values were swallowed by the
  wrapper (body always saw `None`), `athrow` injected into the
  wrapper instead of the body, and `aclose` did not deterministically
  finalise the body or fire the CM hook's `__exit__`.

### Changed (breaking)

- Registry state moved from target-instance attributes
  (`_registered_interceptors`, `_registered_interceptors_id_gen`,
  `_registered_interceptors_originals`) to a module-level
  `WeakKeyDictionary`. Targets no longer carry implementation-detail
  attributes visible in `vars()` / `dir()`. Any code directly
  introspecting the old attributes will need to use
  `get_interceptors` / `has_interceptors` instead.
- `add_interceptor` with an `async def` (or async-generator
  function) as the hook function now raises `TypeError` at
  registration (previously silently returned an unawaited
  coroutine at call time).
- `is_context_manager=True` hooks that return an async context
  manager (`@asynccontextmanager`) now raise `TypeError` with a
  clear message instead of failing with a confusing
  `AttributeError`.
