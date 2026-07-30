#!/usr/bin/env python3
"""Locks the shared fingerprint-keyed SDK launch materialization cache.

Before this fix, `materialize_sdk` copied the provider CLI bundle (up to
hundreds of MB) fresh into the per-run directory on every single turn, with
no cache-hit fast path — the same gap `test_frozen_bundle_cache.py` locks
for the interpreter bundle, but for the CLI/SDK launcher materialization
(`materialize_sdk_launch` / `materialize_claude_sdk_package`). These tests
lock: the stable fingerprint-keyed destination, warm reuse without
re-copying, first-writer-wins race adoption, and fail-closed behavior on a
tampered cache entry — for both native-mode and posix-shebang launches.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home  # noqa: E402

_test_home.isolate(prefix="sdk-launch-cache-")

import provider_pinned_launch  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from paths import ba_home  # noqa: E402
from provider_launch_identity import capture_cli_launch  # noqa: E402
from provider_pinned_launch import (  # noqa: E402
    materialize_sdk_launch_cached,
    sdk_launch_cache_destination,
)


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(path.stat().st_mode | 0o100)


def _native_launch(root: Path, content: bytes = b"native-cli"):
    executable = root / "bin" / "claude"
    _write_executable(executable, content)
    return capture_cli_launch(
        logical_command="claude",
        launcher_path=executable,
        platform="linux",
    )


def _shebang_launch(root: Path, body: bytes = b"print('hi')\n"):
    interpreter = root / "python3"
    _write_executable(interpreter, b"#!/bin/sh\nexit 0\n")
    script = root / "bin" / "claude"
    _write_executable(
        script,
        f"#!{interpreter.resolve()}\n".encode("utf-8") + body,
    )
    return capture_cli_launch(
        logical_command="claude",
        launcher_path=script,
        platform="linux",
    )


def test_destination_is_fingerprint_keyed_under_state_home() -> None:
    with tempfile.TemporaryDirectory(prefix="sdk-cache-dest-") as raw:
        launch = _native_launch(Path(raw))
        destination = sdk_launch_cache_destination(launch)
        expected = (
            ba_home()
            / "cache"
            / "sdk-launches"
            / provider_pinned_launch._sdk_launch_fingerprint(launch)
            / "root"
        )
        assert destination == expected
        assert sdk_launch_cache_destination(launch) == destination
        assert destination.parent.is_dir()
        if os.name != "nt":
            observed = destination.parent.lstat()
            assert (observed.st_mode & 0o777) == 0o700


def test_different_launches_get_different_destinations() -> None:
    with tempfile.TemporaryDirectory(prefix="sdk-cache-distinct-") as raw:
        root = Path(raw)
        first = _native_launch(root / "a", content=b"cli-one")
        second = _native_launch(root / "b", content=b"cli-two")
        assert (
            sdk_launch_cache_destination(first)
            != sdk_launch_cache_destination(second)
        )


def test_native_second_materialization_reuses_cached_copy() -> None:
    with tempfile.TemporaryDirectory(prefix="sdk-cache-native-reuse-") as raw:
        root = Path(raw)
        launch = _native_launch(root)
        first = materialize_sdk_launch_cached(launch)
        assert first.attest()
        first_bytes = Path(first.executable_path).read_bytes()
        # Drift the source after the first materialization: a warm cache
        # hit must serve the attested copy without re-reading the source.
        (root / "bin" / "claude").chmod(0o700)
        (root / "bin" / "claude").write_bytes(b"drifted")
        second = materialize_sdk_launch_cached(launch)
        assert second.executable_path == first.executable_path
        assert Path(second.executable_path).read_bytes() == first_bytes
        assert second.attest()


def test_shebang_second_materialization_reuses_cached_copy() -> None:
    # Unlike the native-mode case, a shebang script's materialized bytes
    # are destination-path-dependent (the shebang line embeds the
    # interpreter's own destination path), so this does not drift the
    # source in place — that would be a genuine identity change the next
    # freshly-captured launch is supposed to catch, not something a warm
    # cache hit should paper over. Instead it captures two independent
    # `AttestedLaunch`es for the same unchanged source (as two real turns
    # would) and asserts the second reuses the first turn's materialized
    # interpreter file rather than rewriting it.
    with tempfile.TemporaryDirectory(prefix="sdk-cache-shebang-reuse-") as raw:
        root = Path(raw)
        first_launch = _shebang_launch(root)
        first = materialize_sdk_launch_cached(first_launch)
        assert first.attest()
        interpreter_path = next(
            path for path in Path(first.executable_path).parent.iterdir()
            if path.name.startswith("provider-runtime")
        )
        interpreter_mtime_ns = interpreter_path.stat().st_mtime_ns

        second_launch = capture_cli_launch(
            logical_command="claude",
            launcher_path=root / "bin" / "claude",
            platform="linux",
        )
        second = materialize_sdk_launch_cached(second_launch)
        assert second.executable_path == first.executable_path
        assert interpreter_path.stat().st_mtime_ns == interpreter_mtime_ns
        assert second.attest()


def test_concurrent_first_materialization_is_serialized_by_lock() -> None:
    # Two threads racing a cold cache miss for the identical fingerprint
    # must not corrupt each other's write: the second in must block on the
    # advisory lock (event-driven wake, no polling) and then adopt the
    # first thread's attested copy rather than writing over it.
    import threading

    with tempfile.TemporaryDirectory(prefix="sdk-cache-concurrent-") as raw:
        root = Path(raw)
        launch = _native_launch(root)
        destination = sdk_launch_cache_destination(launch)
        results: list[object] = [None, None]
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def run(index: int) -> None:
            try:
                start.wait()
                results[index] = materialize_sdk_launch_cached(launch)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert all(result is not None for result in results)
        assert results[0].executable_path == results[1].executable_path
        assert results[0].attest()
        assert destination.is_dir()
        assert not (destination.parent / ".materializing.lock.tmp").exists()


def test_tampered_native_cache_entry_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="sdk-cache-tamper-native-") as raw:
        root = Path(raw)
        launch = _native_launch(root)
        materialized = materialize_sdk_launch_cached(launch)
        target = Path(materialized.executable_path)
        target.chmod(0o700)
        target.write_bytes(b"tampered")
        try:
            materialize_sdk_launch_cached(launch)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered SDK launch cache entry was accepted")


def test_tampered_shebang_cache_entry_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="sdk-cache-tamper-shebang-") as raw:
        root = Path(raw)
        launch = _shebang_launch(root)
        materialized = materialize_sdk_launch_cached(launch)
        target = Path(materialized.executable_path)
        target.chmod(0o700)
        target.write_bytes(b"#!/bin/sh\nexit 1\n")
        try:
            materialize_sdk_launch_cached(launch)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered SDK launch cache entry was accepted")


TESTS = (
    test_destination_is_fingerprint_keyed_under_state_home,
    test_different_launches_get_different_destinations,
    test_native_second_materialization_reuses_cached_copy,
    test_shebang_second_materialization_reuses_cached_copy,
    test_concurrent_first_materialization_is_serialized_by_lock,
    test_tampered_native_cache_entry_fails_closed,
    test_tampered_shebang_cache_entry_fails_closed,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("all SDK launch cache tests passed")
