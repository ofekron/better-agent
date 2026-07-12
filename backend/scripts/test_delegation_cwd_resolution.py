#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchs.manager._delegation import (  # noqa: E402
    CanonicalDelegationCwd,
    _CWD_RESOLVE_CONCURRENCY,
    _DelegationCwdResolver,
    _owns_delegation_cwd_resolver,
)


async def test_dedup_and_loop_responsiveness() -> None:
    resolver = _DelegationCwdResolver()
    original = Path.resolve
    calls = 0

    def slow_resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal calls
        calls += 1
        time.sleep(0.08)
        return original(path, *args, **kwargs)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.005)
            ticks += 1

    with patch.object(Path, "resolve", slow_resolve):
        values, _ = await asyncio.gather(
            asyncio.gather(*(resolver.resolve(".") for _ in range(50))),
            ticker(),
        )
        assert isinstance(values[0], CanonicalDelegationCwd)
        assert len(set(values)) == 1
        assert calls == 1
        assert ticks == 10


async def test_bounded_distinct_resolution_and_cancellation() -> None:
    resolver = _DelegationCwdResolver()
    original = Path.resolve
    active = 0
    maximum = 0
    lock = threading.Lock()

    def slow_resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        result = original(path, *args, **kwargs)
        with lock:
            active -= 1
        return result

    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "resolve", slow_resolve):
        paths = [str(Path(tmp) / f"missing-{index}") for index in range(20)]
        await asyncio.gather(*(resolver.resolve(path) for path in paths))
        assert maximum <= _CWD_RESOLVE_CONCURRENCY

        first = asyncio.create_task(resolver.resolve(str(Path(tmp) / "shared")))
        second = asyncio.create_task(resolver.resolve(str(Path(tmp) / "shared")))
        await asyncio.sleep(0)
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        assert (await second).endswith("shared")


async def test_error_propagates_and_failed_entry_is_retryable() -> None:
    resolver = _DelegationCwdResolver()
    original = Path.resolve
    calls = 0

    def fail_once(path: Path, *args, **kwargs) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("expected resolve failure")
        return original(path, *args, **kwargs)

    with patch.object(Path, "resolve", fail_once):
        try:
            await resolver.resolve(".")
        except OSError as exc:
            assert str(exc) == "expected resolve failure"
        else:
            raise AssertionError("resolve failure was swallowed")
        assert await resolver.resolve(".") == str(original(Path(".")))


async def test_close_drops_projection_and_drains_cancelled_work() -> None:
    resolver = _DelegationCwdResolver()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        link = root / "current"
        link.symlink_to(first, target_is_directory=True)
        assert await resolver.resolve(str(link)) == str(first.resolve())
        await resolver.aclose()
        link.unlink()
        link.symlink_to(second, target_is_directory=True)
        assert await resolver.resolve(str(link)) == str(second.resolve())

    original = Path.resolve
    active = 0
    maximum = 0
    lock = threading.Lock()

    def slow_resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.05)
            return original(path, *args, **kwargs)
        finally:
            with lock:
                active -= 1

    resolver = _DelegationCwdResolver()
    with patch.object(Path, "resolve", slow_resolve):
        waiters = [asyncio.create_task(resolver.resolve(f"missing-{i}")) for i in range(20)]
        await asyncio.sleep(0.01)
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        replacements = [
            asyncio.create_task(resolver.resolve(f"replacement-{i}"))
            for i in range(20)
        ]
        await asyncio.gather(*replacements)
        await resolver.aclose()
        assert maximum <= _CWD_RESOLVE_CONCURRENCY
        assert active == 0
        assert not resolver._in_flight


async def test_structural_owner_closes_on_exception_and_cancellation() -> None:
    seen: list[_DelegationCwdResolver] = []

    @_owns_delegation_cwd_resolver
    async def fail(*, _cwd_resolver: _DelegationCwdResolver) -> None:
        seen.append(_cwd_resolver)
        await _cwd_resolver.resolve(".")
        raise RuntimeError("selection failed")

    try:
        await fail()
    except RuntimeError as exc:
        assert str(exc) == "selection failed"
    else:
        raise AssertionError("selection exception was swallowed")
    assert not seen[-1]._in_flight
    assert not seen[-1]._resolved

    original = Path.resolve
    active = 0
    lock = threading.Lock()

    def slow_resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal active
        with lock:
            active += 1
        try:
            time.sleep(0.05)
            return original(path, *args, **kwargs)
        finally:
            with lock:
                active -= 1

    @_owns_delegation_cwd_resolver
    async def wait(*, _cwd_resolver: _DelegationCwdResolver) -> None:
        seen.append(_cwd_resolver)
        await _cwd_resolver.resolve("cancelled-selection")

    with patch.object(Path, "resolve", slow_resolve):
        task = asyncio.create_task(wait())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert active == 0
    assert not seen[-1]._in_flight
    assert not seen[-1]._resolved


def test_real_process_and_provider_neutrality() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = str(Path(tmp) / "remote-node-only")
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())",
                missing,
            ],
            text=True,
        ).strip()
        assert output == str(Path(missing).resolve())
    source = Path(__file__).resolve().parents[1] / "orchs" / "manager" / "_delegation.py"
    text = source.read_text()
    assert "provider_id" not in text[text.index("class _DelegationCwdResolver"):text.index("def _jsonl_line_has_final_text")]


if __name__ == "__main__":
    asyncio.run(test_dedup_and_loop_responsiveness())
    asyncio.run(test_bounded_distinct_resolution_and_cancellation())
    asyncio.run(test_error_propagates_and_failed_entry_is_retryable())
    asyncio.run(test_close_drops_projection_and_drains_cancelled_work())
    asyncio.run(test_structural_owner_closes_on_exception_and_cancellation())
    test_real_process_and_provider_neutrality()
    print("PASS: delegation CWD resolution is off-loop, bounded, and semantics-preserving")
