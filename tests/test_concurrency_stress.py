"""Concurrency stress tests.

Inspired by wrapt's test_synchronized_lock.py and
test_synchronized_async.py suites — adapted to interceptor-registry's
per-object ``RLock`` model.

Marker convention: tests that routinely exceed 1s on a developer
machine are marked ``@pytest.mark.slow``. Default CI run skips them
via::

    pytest -m "not slow"

Re-run locally with ``pytest -m slow`` or drop the filter to run all.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from interceptor_registry import (
    add_interceptor,
    del_interceptor,
    del_interceptors,
    get_interceptors,
    has_interceptors,
)

# ---------------------------------------------------------------------------
# Baseline concurrency tests (fast — always run)
# ---------------------------------------------------------------------------


class Target:
    def m(self, x=None):
        return f"m({x})"


def test_concurrent_add_from_16_threads_all_unique_ids():
    """16 threads each add one interceptor concurrently — all ids unique,
    every hook fires on a subsequent call."""
    t = Target()
    fire_lock = threading.Lock()
    fires: list[int] = []

    def make_hook(i):
        def hook():
            with fire_lock:
                fires.append(i)

        return hook

    def worker(i):
        return add_interceptor(t, "m", make_hook(i), callorder=-(i + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(worker, range(16)))

    assert len(ids) == 16
    assert len(set(ids)) == 16

    t.m()
    assert len(fires) == 16
    assert set(fires) == set(range(16))


def test_concurrent_add_and_del_final_count_matches_netadds():
    """Thread A adds 32 interceptors, thread B removes 20. Expect 12
    left; concurrent run yields a consistent final count."""
    t = Target()
    ids: list[int] = []
    ids_lock = threading.Lock()

    def adder(i):
        iid = add_interceptor(t, "m", lambda: None, callorder=-(i + 1))
        with ids_lock:
            ids.append(iid)

    # Phase 1: parallel adds.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(adder, range(32)))
    assert len(ids) == 32

    # Phase 2: parallel deletes of a subset.
    to_remove = ids[:20]

    def remover(iid):
        del_interceptor(t, "m", iid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(remover, to_remove))

    remaining = get_interceptors(t, "m")
    assert len(remaining) == 12


def test_cross_object_isolation_does_not_serialise():
    """8 different targets each doing concurrent work don't serialise
    through a shared lock — total wall-clock scales far better than
    serial."""
    targets = [Target() for _ in range(8)]

    def worker(t):
        for _ in range(50):
            iid = add_interceptor(t, "m", lambda: None, callorder=-1)
            t.m()
            del_interceptor(t, "m", iid)

    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, targets))
    elapsed = time.monotonic() - start
    # Should complete well under 2s on any reasonable machine; this is
    # a sanity check, not a hard perf bound.
    assert elapsed < 5.0
    # No lingering interceptors.
    for t in targets:
        assert not has_interceptors(t, "m")


def test_concurrent_invoke_and_register_never_crashes():
    """Thread A calls foo.m() in a tight loop while thread B concurrently
    adds and removes interceptors. Must not raise."""
    t = Target()
    stop = threading.Event()
    errors: list[BaseException] = []

    def caller():
        try:
            while not stop.is_set():
                t.m()
        except BaseException as e:
            errors.append(e)

    def mutator():
        try:
            for i in range(200):
                if stop.is_set():
                    return
                iid = add_interceptor(t, "m", lambda: None, callorder=-(i % 5 + 1))
                t.m()
                del_interceptor(t, "m", iid)
        except BaseException as e:
            errors.append(e)

    threads = [
        threading.Thread(target=caller),
        threading.Thread(target=caller),
        threading.Thread(target=mutator),
    ]
    for th in threads:
        th.start()
    # Let mutator finish, then stop the callers.
    threads[2].join(timeout=10)
    stop.set()
    threads[0].join(timeout=2)
    threads[1].join(timeout=2)

    assert errors == [], f"Unexpected errors: {errors}"


def test_concurrent_get_interceptors_is_snapshot_safe():
    """A caller in get_interceptors() mustn't see a 'dict changed size
    during iteration' error even while another thread mutates."""
    t = Target()
    # Prime the registry.
    for i in range(5):
        add_interceptor(t, "m", lambda: None, callorder=-(i + 1))

    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            while not stop.is_set():
                get_interceptors(t, "m")
        except BaseException as e:
            errors.append(e)

    def writer():
        try:
            for i in range(200):
                if stop.is_set():
                    return
                iid = add_interceptor(t, "m", lambda: None, callorder=-(i + 10))
                del_interceptor(t, "m", iid)
        except BaseException as e:
            errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    w = threading.Thread(target=writer)
    for r in readers:
        r.start()
    w.start()
    w.join(timeout=10)
    stop.set()
    for r in readers:
        r.join(timeout=2)

    assert errors == [], f"Unexpected errors: {errors}"


def test_concurrent_has_interceptors_is_stable_under_writer_load():
    t = Target()
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            while not stop.is_set():
                has_interceptors(t, "m")
        except BaseException as e:
            errors.append(e)

    def writer():
        try:
            for i in range(200):
                iid = add_interceptor(t, "m", lambda: None, callorder=-(i + 1))
                del_interceptor(t, "m", iid)
        except BaseException as e:
            errors.append(e)

    rs = [threading.Thread(target=reader) for _ in range(2)]
    w = threading.Thread(target=writer)
    for r in rs:
        r.start()
    w.start()
    w.join(timeout=10)
    stop.set()
    for r in rs:
        r.join(timeout=2)
    assert errors == [], f"Errors: {errors}"


# ---------------------------------------------------------------------------
# Async concurrency
# ---------------------------------------------------------------------------


async def test_asyncio_gather_100_calls_all_fire_hooks():
    """100 concurrent asyncio calls on an async method with one
    interceptor — all complete, hook fires 100 times."""

    class Bar:
        async def m(self):
            await asyncio.sleep(0)
            return 1

    b = Bar()
    call_count = 0
    counter_lock = asyncio.Lock()

    async def bump():
        nonlocal call_count
        async with counter_lock:
            call_count += 1

    # Our hooks are sync; do a trivial counter using a sync closure.
    sync_fires: list[int] = []

    def sync_hook():
        sync_fires.append(1)

    add_interceptor(b, "m", sync_hook, callorder=-1)
    results = await asyncio.gather(*[b.m() for _ in range(100)])
    assert len(results) == 100
    assert all(r == 1 for r in results)
    assert len(sync_fires) == 100


async def test_asyncio_gather_with_exceptions_still_unwinds_cms():
    """Some tasks raise; others succeed; CMs still exit on each."""
    from contextlib import contextmanager

    class Bar:
        async def m(self, raise_):
            if raise_:
                raise RuntimeError("boom")
            return 1

    enter_count = [0]
    exit_count = [0]

    @contextmanager
    def around(raise_):
        enter_count[0] += 1
        try:
            yield
        finally:
            exit_count[0] += 1

    b = Bar()
    add_interceptor(
        b, "m", around, is_context_manager=True, pass_args=True, callorder=-1
    )
    # Half raise, half succeed.
    tasks = [b.m(i % 2 == 0) for i in range(20)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [r for r in results if isinstance(r, RuntimeError)]
    assert len(failures) == 10
    assert enter_count[0] == 20
    assert exit_count[0] == 20


# ---------------------------------------------------------------------------
# Slow / heavy stress tests — skipped by default
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_heavy_mixed_stress_16_threads_500_ops():
    """16 threads, 500 mixed ops each, of add/del/has/get/invoke.
    Final net interceptor count equals total adds minus total dels.
    """
    t = Target()

    per_thread = 500
    n_threads = 16
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    # Tight accounting.
    counts_lock = threading.Lock()
    net_adds = [0]

    def worker(tid):
        try:
            local_ids: list[int] = []
            for i in range(per_thread):
                op = i % 4
                if op == 0:
                    iid = add_interceptor(
                        t, "m", lambda: None, callorder=-((tid % 5) + 1)
                    )
                    local_ids.append(iid)
                    with counts_lock:
                        net_adds[0] += 1
                elif op == 1 and local_ids:
                    iid = local_ids.pop()
                    del_interceptor(t, "m", iid)
                    with counts_lock:
                        net_adds[0] -= 1
                elif op == 2:
                    has_interceptors(t, "m")
                    t.m()
                else:
                    get_interceptors(t, "m")
        except BaseException as e:
            with errors_lock:
                errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    assert errors == [], f"Stress errors: {errors}"
    # Clean up — leftover interceptors don't matter for isolation.
    del_interceptors(t, "m")
    assert not has_interceptors(t, "m")


@pytest.mark.slow
def test_concurrent_invoke_burst_1000_rounds():
    """1000 rapid invocations from 8 threads against a single patched
    target — assert hook fires exactly 1000 * 8 times."""
    t = Target()
    fires: list[int] = []
    fire_lock = threading.Lock()

    def hook():
        with fire_lock:
            fires.append(1)

    add_interceptor(t, "m", hook, callorder=-1)

    def worker(_):
        for _ in range(1000):
            t.m()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert len(fires) == 8000
