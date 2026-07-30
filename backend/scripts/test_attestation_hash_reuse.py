#!/usr/bin/env python3
"""Locks the launch-attestation hash-reuse perf fix.

Before this fix, a single provider launch re-hashed the same large,
unchanged files repeatedly across capture/attest/materialize layer
boundaries: `codex_execution_common.cached_sha256_fd` (keyed by resolved
path + stable stat tuple) is the single primitive every full-file hash
now funnels through, and `AttestOnceCache` collapses the repeated
`FamilyLaunchAttestation`/`RunnerLaunch` `.attest()` calls at each layer
boundary into one real hash pass followed by cheap stat-tuple rechecks.

These tests lock:
  - a cache hit reuses a prior digest without re-reading the file;
  - a stat-tuple change is always a cache miss (never silently reused);
  - `FamilyLaunchAttestation.attest()` only fully re-hashes once per
    instance - `open_downstream()`/`materialize_sdk()`/`open_runner()`
    afterward degrade to a stat-only recheck;
  - a stat mismatch after the first full attest fails closed rather
    than falling back to a rehash-and-accept;
  - a frozen-bundle materialization cache hit skips the expensive
    per-file rehash (no `parallel_map_processes` re-invocation) once a
    destination has already been verified once in this process;
  - the materialized Claude Agent SDK package cache reuses a prior
    turn's copy and still fails closed on a tampered cache entry.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

import _test_home  # noqa: E402

_test_home.isolate(prefix="attestation-hash-reuse-")

import codex_execution_common  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from codex_execution_identity import FileIdentity  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    FamilyLaunchAttestation,
    capture_cli_launch,
    capture_config_scope,
    capture_critical_package,
    capture_runner_launch,
)
from provider_frozen_bundle import (  # noqa: E402
    FrozenBundleIdentity,
    frozen_bundle_destination,
    materialize_frozen_bundle,
)
from provider_claude_execution import (  # noqa: E402
    materialize_claude_sdk_package_cached,
)


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(path.stat().st_mode | 0o100)


def _count_real_hashes(monkeypatch_calls: list[int]):
    """Patch the two raw, uncached full-file hashers a cache miss falls
    back to (`sha256_fd` for `cached_sha256_fd`, and
    `sha256_and_first_line_fd` for the shebang/first-line variant), so
    tests can count real full-file reads regardless of which path took
    them.

    `sha256_and_first_line_fd` is imported by name (`from
    codex_execution_common import sha256_and_first_line_fd`) into both
    codex_execution_identity and codex_execution_launch, so each of those
    modules holds its own independent reference to the original function
    - patching only the attribute on codex_execution_common would leave
    those call sites hashing for real, uncounted. All three bindings are
    patched and restored together.
    """
    import codex_execution_identity
    import codex_execution_launch

    original_sha256_fd = codex_execution_common.sha256_fd
    original_first_line = codex_execution_common.sha256_and_first_line_fd

    def _counting_sha256_fd(fd: int) -> str:
        monkeypatch_calls.append(1)
        return original_sha256_fd(fd)

    def _counting_first_line(fd: int) -> tuple[str, bytes]:
        monkeypatch_calls.append(1)
        return original_first_line(fd)

    codex_execution_common.sha256_fd = _counting_sha256_fd
    codex_execution_common.sha256_and_first_line_fd = _counting_first_line
    codex_execution_identity.sha256_and_first_line_fd = _counting_first_line
    codex_execution_launch.sha256_and_first_line_fd = _counting_first_line
    return (original_sha256_fd, original_first_line)


def _restore_real_hashes(original) -> None:
    import codex_execution_identity
    import codex_execution_launch

    original_sha256_fd, original_first_line = original
    codex_execution_common.sha256_fd = original_sha256_fd
    codex_execution_common.sha256_and_first_line_fd = original_first_line
    codex_execution_identity.sha256_and_first_line_fd = original_first_line
    codex_execution_launch.sha256_and_first_line_fd = original_first_line


def test_cached_sha256_fd_reuses_digest_for_unchanged_stat() -> None:
    calls: list[int] = []
    original = _count_real_hashes(calls)
    try:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "payload.bin"
            target.write_bytes(b"a" * (64 * 1024))
            first = FileIdentity.capture(target)
            assert len(calls) == 1
            second = FileIdentity.capture(target)
            assert len(calls) == 1, "unchanged file was re-hashed"
            assert first.sha256 == second.sha256
            # A real content change (new stat: size + mtime/ctime differ)
            # must always be a cache miss, never a stale reuse.
            target.write_bytes(b"b" * (32 * 1024))
            third = FileIdentity.capture(target)
            assert len(calls) == 2, "changed file incorrectly reused a cached digest"
            assert third.sha256 != first.sha256
    finally:
        _restore_real_hashes(original)


def test_recapture_cli_launch_in_same_process_reuses_cache() -> None:
    """Locks the exact regression a same-process benchmark caught:
    `capture_cli_launch` used `FileIdentity.capture_with_first_line`,
    whose hasher called `sha256_and_first_line_fd` directly and never
    consulted/populated the shared hash cache - so a second capture of an
    unchanged launcher (e.g. `prepare_run` on a later turn, same backend
    process) always re-hashed the file from scratch. This is what makes a
    warm turn's launch preparation cheap after the first turn."""
    calls: list[int] = []
    original = _count_real_hashes(calls)
    try:
        with tempfile.TemporaryDirectory() as raw:
            cli = Path(raw) / "bin" / "claude"
            _write_executable(cli, b"native-cli-payload" * 4096)
            calls.clear()
            first = capture_cli_launch(
                logical_command="claude",
                launcher_path=cli,
                platform="linux",
            )
            after_first_capture = len(calls)
            assert after_first_capture > 0, "first capture did no verification"
            second = capture_cli_launch(
                logical_command="claude",
                launcher_path=cli,
                platform="linux",
            )
            assert len(calls) == after_first_capture, (
                "recapturing an unchanged CLI launcher in the same process "
                "re-hashed it instead of reusing the process-wide cache"
            )
            assert first.launcher.sha256 == second.launcher.sha256
    finally:
        _restore_real_hashes(original)


def _capture_family(root: Path) -> FamilyLaunchAttestation:
    run_dir = root / "run"
    run_dir.mkdir()
    runner = root / "backend" / "runner.py"
    _write_executable(runner, b"print('runner')\n")
    cli = root / "bin" / "claude"
    _write_executable(cli, b"native-cli-payload" * 1024)
    config_root = root / "config"
    config_root.mkdir()
    settings = config_root / "settings.json"
    settings.write_text('{"theme":"dark"}', encoding="utf-8")
    package_root = root / "site-packages" / "claude_agent_sdk"
    _write_executable(package_root / "__init__.py", b"__version__='1'\n")
    return FamilyLaunchAttestation.capture(
        family="claude",
        runner=capture_runner_launch(
            run_dir=run_dir,
            executable_path=Path(sys.executable),
            runner_entry=runner,
            runner_kind="claude",
            runner_module="runner",
            frozen=False,
        ),
        downstream=capture_cli_launch(
            logical_command="claude",
            launcher_path=cli,
            platform="linux",
        ),
        config=capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
        ),
        critical_packages=(
            capture_critical_package(
                package_name="claude_agent_sdk",
                package_root=package_root,
                relative_paths=("__init__.py",),
            ),
        ),
    )


def test_repeat_family_attest_collapses_to_one_full_hash_pass() -> None:
    calls: list[int] = []
    original = _count_real_hashes(calls)
    try:
        with tempfile.TemporaryDirectory() as raw:
            calls.clear()
            attestation = _capture_family(Path(raw))
            after_capture = len(calls)
            assert after_capture > 0, "capture() did no verification at all"
            # `.attest()` - including its FIRST call - must not add any
            # additional real hash reads: capture() already populated the
            # process-wide cache for every component it touched (the
            # launcher via capture_with_first_line, everything else via
            # plain capture()), so re-verifying the same (path, stat) is a
            # cache hit end to end, not just on the second call onward.
            for _ in range(6):
                assert attestation.attest()
            assert len(calls) == after_capture, (
                "attest() performed additional full-file hashing beyond "
                "what capture() already did"
            )
    finally:
        _restore_real_hashes(original)


def test_stat_drift_after_full_attest_fails_closed_without_rehash() -> None:
    calls: list[int] = []
    original = _count_real_hashes(calls)
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            attestation = _capture_family(root)
            assert attestation.attest()
            calls.clear()
            cli = root / "bin" / "claude"
            # Drift content, but the cheap recheck must catch this via the
            # stat tuple alone (mtime/ctime/size change) - it must not fall
            # back to a rehash that could paper over the swap.
            cli.chmod(0o700)
            cli.write_bytes(b"tampered-cli-payload" * 1024)
            assert not attestation.attest()
            assert len(calls) == 0, (
                "stat-mismatch recheck fell back to a full rehash "
                "instead of failing closed on the stat tuple alone"
            )
    finally:
        _restore_real_hashes(original)


def test_family_attestation_open_downstream_reuses_one_full_hash() -> None:
    calls: list[int] = []
    original = _count_real_hashes(calls)
    try:
        with tempfile.TemporaryDirectory() as raw:
            attestation = _capture_family(Path(raw))
            calls.clear()
            with attestation.open_downstream():
                pass
            after_open = len(calls)
            assert after_open > 0
            # A second open (same instance, nothing changed) materializes a
            # fresh private destination copy again (by design - each open
            # is a new per-run pin), so it still pays exactly one fresh
            # destination hash per launch component; what it must NOT pay
            # again is a second full source-side re-verification (the
            # attest() re-check, and the source half of the copy verify).
            # A regression back to hashing the source a second time (the
            # original double-hash-per-copy bug) would show up here as
            # more than one additional real hash per component.
            calls.clear()
            with attestation.open_downstream():
                pass
            component_count = len(attestation.downstream.components)
            assert 0 < len(calls) <= component_count, (
                f"expected at most {component_count} fresh destination "
                f"hash(es) on a repeat open, got {len(calls)}"
            )
    finally:
        _restore_real_hashes(original)


def _capture_bundle(root: Path) -> FrozenBundleIdentity:
    executable = root / "bin" / "python"
    sidecar = root / "lib"
    sidecar.mkdir(parents=True)
    (sidecar / "stdlib_module.py").write_text("x = 1\n", encoding="utf-8")
    _write_executable(executable, b"python-runtime-bytes" * 4096)
    return FrozenBundleIdentity.capture(
        executable_path=executable,
        bundle_root=root,
        sidecar_root=sidecar,
    )


def test_frozen_bundle_materialization_skips_rehash_on_warm_adoption() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bundle = _capture_bundle(root)
        destination = frozen_bundle_destination(bundle)
        first = materialize_frozen_bundle(bundle, destination)
        assert first == destination.resolve(strict=True)

        calls: list[int] = []
        original_map = codex_execution_common.parallel_map_processes

        def _counting_map(fn, items):
            calls.append(1)
            return original_map(fn, items)

        codex_execution_common.parallel_map_processes = _counting_map
        try:
            second = materialize_frozen_bundle(bundle, destination)
            assert second == first
            assert not calls, (
                "warm frozen-bundle adoption re-invoked the expensive "
                "per-file subprocess-parallel rehash instead of trusting "
                "the previously-verified destination stat tuple"
            )
        finally:
            codex_execution_common.parallel_map_processes = original_map

        # Tampering the destination after it was memoized as verified must
        # still fail closed - the memo can only skip a rehash of an
        # unmodified destination, never accept a changed one.
        materialized_executable = destination.resolve(strict=True) / "bin" / "python"
        materialized_executable.chmod(0o700)
        materialized_executable.write_bytes(b"tampered-runtime-bytes")
        try:
            materialize_frozen_bundle(bundle, destination)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered frozen bundle destination was adopted")


def test_claude_sdk_package_cached_reuses_materialized_copy() -> None:
    from provider_family_launch_attestation import CriticalPackageIdentity

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        package_root = root / "claude_agent_sdk"
        _write_executable(package_root / "__init__.py", b"__version__='1'\n")
        package: CriticalPackageIdentity = capture_critical_package(
            package_name="claude_agent_sdk",
            package_root=package_root,
            relative_paths=("__init__.py",),
        )
        first = materialize_claude_sdk_package_cached(package)
        first_bytes = (first / "__init__.py").read_bytes()
        # Drift the source after materialization: a warm cache hit must
        # serve the already-attested copy without re-reading the source.
        (package_root / "__init__.py").chmod(0o700)
        (package_root / "__init__.py").write_bytes(b"__version__='drifted'\n")
        second = materialize_claude_sdk_package_cached(package)
        assert second == first
        assert (second / "__init__.py").read_bytes() == first_bytes

        # A tampered cache entry must fail closed rather than being reused.
        (first / "__init__.py").chmod(0o700)
        (first / "__init__.py").write_bytes(b"__version__='tampered'\n")
        try:
            materialize_claude_sdk_package_cached(package)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered SDK package cache entry was accepted")


TESTS = (
    test_cached_sha256_fd_reuses_digest_for_unchanged_stat,
    test_recapture_cli_launch_in_same_process_reuses_cache,
    test_repeat_family_attest_collapses_to_one_full_hash_pass,
    test_stat_drift_after_full_attest_fails_closed_without_rehash,
    test_family_attestation_open_downstream_reuses_one_full_hash,
    test_frozen_bundle_materialization_skips_rehash_on_warm_adoption,
    test_claude_sdk_package_cached_reuses_materialized_copy,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("all attestation hash-reuse tests passed")
