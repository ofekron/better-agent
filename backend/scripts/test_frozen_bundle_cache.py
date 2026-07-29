#!/usr/bin/env python3
"""Locks the shared fingerprint-keyed frozen bundle cache.

Before the fix, `frozen_bundle_destination` was keyed by the per-run
directory, so the cache-hit fast path in `materialize_frozen_bundle`
could never fire and every turn paid a full bundle copy. These tests
lock: the stable fingerprint-keyed destination, warm reuse without
re-reading the source, first-writer-wins race adoption, and fail-closed
behavior on a tampered cache entry.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home

_test_home.isolate(prefix="frozen-bundle-cache-")

import provider_frozen_bundle  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from paths import ba_home  # noqa: E402
from provider_frozen_bundle import (  # noqa: E402
    attest_materialized_frozen_bundle,
    frozen_bundle_destination,
    materialize_frozen_bundle,
    remove_materialized_bundle,
)
from test_frozen_onedir_artifact import _capture  # noqa: E402


def test_destination_is_fingerprint_keyed_under_state_home() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-cache-dest-") as raw:
        bundle, root, _ = _capture(Path(raw))
        destination = frozen_bundle_destination(bundle)
        expected = (
            ba_home()
            / "cache"
            / "frozen-bundles"
            / bundle.fingerprint
            / root.name
        )
        assert destination == expected
        assert frozen_bundle_destination(bundle) == destination
        assert destination.parent.is_dir()
        if os.name != "nt":
            observed = destination.parent.lstat()
            assert (observed.st_mode & 0o777) == 0o700


def test_materialized_files_preserve_source_mtime() -> None:
    # A fresh mtime marks shipped __pycache__ entries stale, so executing
    # the cached runtime would regenerate them in place and break the
    # next turn's attestation of the shared cache entry.
    with tempfile.TemporaryDirectory(prefix="frozen-cache-mtime-") as raw:
        bundle, _, _ = _capture(Path(raw))
        destination = materialize_frozen_bundle(
            bundle,
            frozen_bundle_destination(bundle),
        )
        for entry in bundle.entries:
            if entry.file is None:
                continue
            observed = (destination / entry.relative_path).lstat()
            assert observed.st_mtime_ns == entry.file.mtime_ns, (
                entry.relative_path
            )


def test_second_materialization_reuses_cached_copy() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-cache-reuse-") as raw:
        bundle, root, _ = _capture(Path(raw))
        first = materialize_frozen_bundle(
            bundle,
            frozen_bundle_destination(bundle),
        )
        # Drift the source after the first materialization: a warm cache
        # hit must serve the attested copy without re-reading the source.
        entry = next(
            candidate for candidate in bundle.entries
            if candidate.kind == "file"
            and candidate.relative_path != bundle.executable_relative
        )
        source = root / entry.relative_path
        source.chmod(0o700)
        source.write_bytes(b"source-drift")
        second = materialize_frozen_bundle(
            bundle,
            frozen_bundle_destination(bundle),
        )
        assert second == first
        assert attest_materialized_frozen_bundle(bundle, second)


def test_rename_race_adopts_winning_copy() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-cache-race-") as raw:
        parent = Path(raw)
        bundle, _, _ = _capture(parent)
        destination = frozen_bundle_destination(bundle)
        scratch = parent / "winner"
        scratch.mkdir(mode=0o700)
        winner_copy = materialize_frozen_bundle(
            bundle,
            scratch / destination.name,
        )
        original_rename = os.rename
        raced: list[bool] = []

        def racing_rename(source, target, *args, **kwargs):
            if Path(target) == destination and not raced:
                raced.append(True)
                original_rename(winner_copy, target)
            return original_rename(source, target, *args, **kwargs)

        provider_frozen_bundle.os.rename = racing_rename
        try:
            adopted = materialize_frozen_bundle(bundle, destination)
        finally:
            provider_frozen_bundle.os.rename = original_rename
        assert raced
        assert adopted == destination.resolve()
        assert attest_materialized_frozen_bundle(bundle, destination)
        leftovers = [
            path for path in destination.parent.iterdir()
            if path.name.startswith(".frozen-runner.")
        ]
        assert leftovers == []


def test_tampered_cache_entry_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-cache-tamper-") as raw:
        bundle, _, _ = _capture(Path(raw))
        destination = materialize_frozen_bundle(
            bundle,
            frozen_bundle_destination(bundle),
        )
        entry = next(
            candidate for candidate in bundle.entries
            if candidate.kind == "file"
            and candidate.relative_path != bundle.executable_relative
        )
        tampered = destination / entry.relative_path
        tampered.chmod(0o700)
        tampered.write_bytes(b"tampered")
        try:
            materialize_frozen_bundle(
                bundle,
                frozen_bundle_destination(bundle),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered cache entry was accepted")
        # Removing the corrupt entry re-enables a fresh materialization.
        remove_materialized_bundle(destination)
        rebuilt = materialize_frozen_bundle(
            bundle,
            frozen_bundle_destination(bundle),
        )
        assert attest_materialized_frozen_bundle(bundle, rebuilt)


TESTS = (
    test_destination_is_fingerprint_keyed_under_state_home,
    test_materialized_files_preserve_source_mtime,
    test_second_materialization_reuses_cached_copy,
    test_rename_race_adopts_winning_copy,
    test_tampered_cache_entry_fails_closed,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("all frozen bundle cache tests passed")
