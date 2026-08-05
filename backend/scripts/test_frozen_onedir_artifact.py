#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_execution_common import ExecutionContractError
from codex_execution_identity import FileIdentity
import provider_frozen_artifact_smoke
import provider_frozen_bundle
import private_diagnostics
from provider_claude_execution import (
    attest_embedded_claude_sdk,
    capture_embedded_claude_sdk,
)
from provider_frozen_artifact_smoke import main as artifact_smoke_main
from provider_frozen_bundle import (
    FrozenBundleIdentity,
    _copied_descriptor_matches_identity,
    attest_materialized_frozen_bundle,
    materialize_frozen_bundle,
)
from provider_runner_launch import capture_runner_launch

ROOT = Path(__file__).resolve().parents[2]
_FROZEN_EXECUTABLE = b"MZ\r\n\x1a\nfrozen\r\nexecutable\n"


def _fixture(parent: Path) -> tuple[Path, Path, Path]:
    root = parent / "Better Agent"
    sidecar = root / "_internal"
    sidecar.mkdir(parents=True)
    executable = root / (
        "Better Agent.exe" if sys.platform == "win32" else "Better Agent"
    )
    executable.write_bytes(_FROZEN_EXECUTABLE)
    executable.chmod(0o500)
    package = sidecar / "claude_agent_sdk"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"SDK_AUTHORITY = True\n")
    metadata = sidecar / "claude_agent_sdk-0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_bytes(b"Name: claude-agent-sdk\n")
    (sidecar / "runner_agy.pyc").write_bytes(b"agy-runner")
    if os.name != "nt":
        os.symlink(
            "claude_agent_sdk/__init__.py",
            sidecar / "sdk-link",
        )
    return root, executable, sidecar


def _capture(parent: Path) -> tuple[
    FrozenBundleIdentity,
    Path,
    Path,
]:
    root, executable, sidecar = _fixture(parent)
    bundle = FrozenBundleIdentity.capture(
        executable_path=executable,
        bundle_root=root,
        sidecar_root=sidecar,
    )
    return bundle, root, sidecar


def test_complete_bundle_round_trip_and_materialization() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-bundle-") as raw:
        parent = Path(raw)
        bundle, root, _ = _capture(parent)
        restored = FrozenBundleIdentity.from_dict(
            json.loads(json.dumps(bundle.to_dict())),
        )
        assert restored == bundle
        assert restored.attest()
        run_dir = parent / "run"
        run_dir.mkdir(mode=0o700)
        destination = run_dir / "frozen-runner"
        previous_umask = os.umask(0o077)
        try:
            materialized = materialize_frozen_bundle(
                restored,
                destination,
            )
        finally:
            os.umask(previous_umask)
        assert materialized == destination.resolve()
        assert attest_materialized_frozen_bundle(restored, destination)
        assert (
            destination / restored.executable_relative
        ).read_bytes() == _FROZEN_EXECUTABLE
        assert materialize_frozen_bundle(restored, destination) == materialized
        source_entry = next(
            entry for entry in restored.entries
            if entry.kind == "file"
            and entry.relative_path != restored.executable_relative
        )
        source = root / source_entry.relative_path
        source.chmod(source_entry.mode | stat.S_IWUSR)
        source.write_bytes(b"source-drift")
        assert materialize_frozen_bundle(
            restored,
            destination,
        ) == materialized
        source_paths = {
            entry.relative_path for entry in restored.entries
        }
        target_paths = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
        }
        assert target_paths == source_paths
        assert root.exists()


def test_embedded_sdk_binding_is_independent_of_live_bundle_state() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-sdk-binding-") as raw:
        parent = Path(raw)
        root, executable, sidecar = _fixture(parent)
        run_dir = parent / "run"
        run_dir.mkdir()
        runner = capture_runner_launch(
            run_dir=run_dir,
            executable_path=executable,
            runner_entry=Path(__file__),
            runner_kind="claude",
            runner_module="runner",
            frozen=True,
            frozen_bundle_root=root,
            frozen_sidecar_root=sidecar,
        )
        package = capture_embedded_claude_sdk(runner)
        executable.chmod(0o700)
        executable.write_bytes(b"drifted-frozen-executable")
        assert not runner.attest()
        assert attest_embedded_claude_sdk(package, runner)


def test_descriptor_copy_accepts_metadata_only_drift() -> None:
    with tempfile.TemporaryDirectory(prefix="descriptor-copy-") as raw:
        source = Path(raw) / "source"
        source.write_bytes(b"descriptor-bound-content")
        identity = FileIdentity.capture(source)
        descriptor = os.open(source, os.O_RDONLY)
        try:
            before = os.fstat(descriptor)
            digest = hashlib.sha256(os.read(descriptor, identity.size)).hexdigest()
            os.utime(
                source,
                ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
            )
            after = os.fstat(descriptor)
            assert not identity.attest_metadata()
            assert _copied_descriptor_matches_identity(
                before,
                after,
                digest,
                identity,
            )
            assert not _copied_descriptor_matches_identity(
                before,
                after,
                "0" * 64,
                identity,
            )
        finally:
            os.close(descriptor)


def test_macos_default_bundle_root_preserves_app_layout() -> None:
    if sys.platform != "darwin":
        return
    with tempfile.TemporaryDirectory(prefix="mac-frozen-bundle-") as raw:
        app = Path(raw) / "Better Agent.app"
        executable = app / "Contents" / "MacOS" / "Better Agent"
        sidecar = app / "Contents" / "Frameworks"
        executable.parent.mkdir(parents=True)
        sidecar.mkdir(parents=True)
        executable.write_bytes(b"frozen-executable")
        executable.chmod(0o500)
        (sidecar / "runner.pyc").write_bytes(b"runner")
        old_frozen = getattr(sys, "frozen", None)
        old_meipass = getattr(sys, "_MEIPASS", None)
        had_frozen = hasattr(sys, "frozen")
        had_meipass = hasattr(sys, "_MEIPASS")
        sys.frozen = True
        sys._MEIPASS = str(sidecar)
        try:
            bundle = FrozenBundleIdentity.capture(
                executable_path=executable,
            )
        finally:
            if had_frozen:
                sys.frozen = old_frozen
            else:
                del sys.frozen
            if had_meipass:
                sys._MEIPASS = old_meipass
            else:
                del sys._MEIPASS
        assert Path(bundle.root.resolved_path) == app.resolve()
        assert bundle.executable_relative == (
            "Contents/MacOS/Better Agent"
        )
        assert bundle.sidecar_relative == "Contents/Frameworks"


def test_artifact_workflow_installs_backend_relative_requirements() -> None:
    workflow = yaml.safe_load(
        (
            ROOT
            / ".github"
            / "workflows"
            / "immutable-family-artifact-smoke.yml"
        ).read_text(encoding="utf-8"),
    )
    steps = workflow["jobs"]["frozen-onedir-smoke"]["steps"]
    install = next(
        step
        for step in steps
        if step.get("name") == "Install frozen build dependencies"
    )
    assert install["working-directory"] == "backend"
    assert "-r requirements.txt" in install["run"]
    assert "-r requirements-claude.txt" in install["run"]
    assert "-r backend/" not in install["run"]
    windows_smoke = next(
        step for step in steps if step.get("name") == "Smoke Windows artifact"
    )
    assert "Start-Process" in windows_smoke["run"]
    assert "-PassThru" in windows_smoke["run"]
    assert "$Smoke.WaitForExit(1000)" in windows_smoke["run"]
    assert "[DateTime]::UtcNow.AddMinutes(5)" in windows_smoke["run"]
    assert "taskkill.exe" in windows_smoke["run"]
    assert "/T" in windows_smoke["run"]
    assert "/F" in windows_smoke["run"]
    assert "$Smoke.WaitForExit(5000)" in windows_smoke["run"]
    assert "$Smoke.WaitForExit()" not in windows_smoke["run"]
    assert windows_smoke["run"].count("Show-SmokeDiagnostics") == 3
    assert 'Filter "result.progress.*.json"' in windows_smoke["run"]
    assert "ConvertFrom-Json" in windows_smoke["run"]
    assert "artifact-smoke-progress" in windows_smoke["run"]
    assert "$Smoke.ExitCode" in windows_smoke["run"]
    assert "$LASTEXITCODE" not in windows_smoke["run"]
    assert "state\\faulthandler.log" in windows_smoke["run"]
    upload = next(
        step for step in steps
        if step.get("name") == "Upload artifact smoke diagnostics"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["path"].endswith(
        "/better-agent-artifact-smoke/",
    )


def test_frozen_bundle_excludes_optional_mcp_cli_surface() -> None:
    spec = (ROOT / "desktop" / "BetterAgent.spec").read_text(
        encoding="utf-8",
    )
    assert 'not _module_name.startswith("mcp.cli.")' in spec
    assert "filter_submodules=_without_optional_mcp_cli" in spec


def test_windows_materialization_uses_acl_authority() -> None:
    paths_source = (
        ROOT / "backend" / "paths.py"
    ).read_text(encoding="utf-8")
    bundle_source = (
        ROOT / "backend" / "provider_frozen_bundle.py"
    ).read_text(encoding="utf-8")
    sdk_source = (
        ROOT / "backend" / "provider_claude_execution.py"
    ).read_text(encoding="utf-8")
    smoke_source = (
        ROOT / "backend" / "provider_frozen_artifact_smoke.py"
    ).read_text(encoding="utf-8")
    for source in (bundle_source, sdk_source):
        assert 'if os.name == "nt"' in source
        assert "windows_path_has_private_acl(" in source
        assert "require_protected=False" not in source
    pinned_source = (
        ROOT / "backend" / "provider_pinned_launch.py"
    ).read_text(encoding="utf-8")
    assert "make_private_directory(run_dir)" in pinned_source
    assert "make_private_directory(container)" in sdk_source
    assert "SetNamedSecurityInfoW" in paths_source
    assert "GetNamedSecurityInfoW" in paths_source
    assert '"powershell"' not in paths_source
    assert '"icacls"' not in paths_source
    assert "state_root = ba_home()" in smoke_source
    assert "dir=state_root" in smoke_source
    for stage in (
        "state-root",
        "temp-root",
        "snapshot-root",
        "capture",
        "materialize",
        "tamper",
        "windows-wrapper",
        "temp-cleanup",
    ):
        assert f'stage="{stage}"' in smoke_source
    assert 'stage=f"probe:{family}"' in smoke_source
    assert smoke_source.count("open_pinned_runner_launch(") == 1
    assert '"timings_ms": timings' in smoke_source
    assert "os.fsync(output)" not in bundle_source


def test_artifact_smoke_failure_is_structured() -> None:
    with tempfile.TemporaryDirectory(prefix="artifact-failure-") as raw:
        output = Path(raw) / "result.json"
        result = artifact_smoke_main(
            ["--frozen-artifact-smoke", "--output", str(output)],
        )
        assert result == 1
        assert json.loads(output.read_text(encoding="utf-8")) == {
            "error": "artifact smoke requires frozen runtime",
        }
        progress = sorted(output.parent.glob("result.progress.*.json"))
        assert [
            json.loads(path.read_text(encoding="utf-8"))
            for path in progress
        ] == [
            {"stage": "smoke", "status": "started"},
            {
                "error": "artifact smoke requires frozen runtime",
                "stage": "smoke",
                "status": "failed",
            },
        ]


def test_materialization_normalizes_operational_exceptions() -> None:
    with tempfile.TemporaryDirectory(prefix="materialization-errors-") as raw:
        bundle, _, _ = _capture(Path(raw))
        destination = Path(raw) / "destination"
        for failure in (
            OSError("private filesystem detail"),
            RuntimeError("private runtime detail"),
            AttributeError("private platform detail"),
        ):
            with mock.patch.object(
                provider_frozen_bundle,
                "_materialize_frozen_bundle",
                side_effect=failure,
            ):
                try:
                    materialize_frozen_bundle(bundle, destination)
                except ExecutionContractError as exc:
                    assert str(exc) == "frozen bundle materialization failed"
                    assert exc.__cause__ is failure
                else:
                    raise AssertionError(
                        f"{type(failure).__name__} escaped materialization",
                    )
        with mock.patch.object(
            provider_frozen_bundle,
            "_materialize_frozen_bundle",
            side_effect=KeyboardInterrupt(),
        ):
            try:
                materialize_frozen_bundle(bundle, destination)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("KeyboardInterrupt was normalized")


def test_artifact_smoke_unexpected_failure_is_structured() -> None:
    with tempfile.TemporaryDirectory(prefix="artifact-unexpected-") as raw:
        root = Path(raw)
        state_root = root / "state"
        state_root.mkdir()
        output = root / "result.json"
        failure = RuntimeError("private runtime detail")
        with (
            mock.patch.object(
                provider_frozen_artifact_smoke,
                "ba_home",
                return_value=state_root,
            ),
            mock.patch.object(
                private_diagnostics,
                "ba_home",
                return_value=state_root,
            ),
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(root), create=True),
            mock.patch.object(
                provider_frozen_artifact_smoke,
                "_materialize_snapshot",
                side_effect=failure,
            ),
        ):
            result = artifact_smoke_main(
                ["--frozen-artifact-smoke", "--output", str(output)],
            )
        assert result == 1
        assert json.loads(output.read_text(encoding="utf-8")) == {
            "error": "artifact smoke failed",
            "exception_type": "RuntimeError",
        }
        checkpoints = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(root.glob("result.progress.*.json"))
        ]
        assert {"stage": "temp-cleanup", "status": "completed"} in checkpoints
        assert checkpoints[-1] == {
            "error": "artifact smoke failed (RuntimeError)",
            "stage": "smoke",
            "status": "failed",
        }
        assert "private runtime detail" not in json.dumps(checkpoints)
        diagnostic = (state_root / "faulthandler.log").read_text(
            encoding="utf-8",
        )
        assert "RuntimeError: private runtime detail" in diagnostic


def test_private_diagnostic_log_rejects_redirects() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="private-diagnostics-") as raw:
        root = Path(raw)
        victim = root / "victim"
        victim.write_text("unchanged", encoding="utf-8")
        (root / "faulthandler.log").symlink_to(victim)
        with mock.patch.object(private_diagnostics, "ba_home", return_value=root):
            try:
                private_diagnostics.append_private_exception(
                    RuntimeError("must not reach victim"),
                    context="test",
                )
            except OSError:
                pass
            else:
                raise AssertionError("redirecting diagnostic path was accepted")
        assert victim.read_text(encoding="utf-8") == "unchanged"


def test_private_diagnostic_log_preserves_exception_chain() -> None:
    with tempfile.TemporaryDirectory(prefix="diagnostic-chain-") as raw:
        root = Path(raw)
        with mock.patch.object(private_diagnostics, "ba_home", return_value=root):
            try:
                try:
                    raise OSError("inner operation detail")
                except OSError as cause:
                    raise ExecutionContractError("outer contract") from cause
            except ExecutionContractError as exc:
                private_diagnostics.append_private_exception(
                    exc,
                    context="test chain",
                )
        diagnostic = (root / "faulthandler.log").read_text(encoding="utf-8")
        assert "OSError: inner operation detail" in diagnostic
        assert "ExecutionContractError: outer contract" in diagnostic
        if os.name != "nt":
            assert stat.S_IMODE(
                (root / "faulthandler.log").stat().st_mode,
            ) == 0o600


def test_source_add_change_remove_and_mode_tamper_are_rejected() -> None:
    def truncate(root: Path, sidecar: Path) -> None:
        target = sidecar / "claude_agent_sdk" / "__init__.py"
        target.write_bytes(b"x")

    def replace(root: Path, sidecar: Path) -> None:
        target = sidecar / "runner_agy.pyc"
        contents = target.read_bytes()
        target.unlink()
        target.write_bytes(contents)

    mutations = (
        lambda root, sidecar: (sidecar / "injected.py").write_bytes(b"x"),
        lambda root, sidecar: (
            sidecar / "claude_agent_sdk" / "__init__.py"
        ).write_bytes(b"changed"),
        truncate,
        replace,
        lambda root, sidecar: (
            sidecar / "runner_agy.pyc"
        ).unlink(),
        lambda root, sidecar: (
            root / (
                "Better Agent.exe"
                if sys.platform == "win32"
                else "Better Agent"
            )
        ).chmod(0o700),
    )
    for mutate in mutations:
        with tempfile.TemporaryDirectory(prefix="frozen-tamper-") as raw:
            bundle, root, sidecar = _capture(Path(raw))
            mutate(root, sidecar)
            assert not bundle.attest()
            run_dir = Path(raw) / "run"
            run_dir.mkdir(mode=0o700)
            try:
                materialize_frozen_bundle(
                    bundle,
                    run_dir / "frozen-runner",
                )
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("source tamper was accepted")


def test_materialized_add_change_remove_and_mode_tamper_are_rejected() -> None:
    def mutate_add(target: Path, bundle: FrozenBundleIdentity) -> None:
        (target / "injected.py").write_bytes(b"x")

    def mutate_change(target: Path, bundle: FrozenBundleIdentity) -> None:
        executable = target / bundle.executable_relative
        executable.chmod(0o700)
        executable.write_bytes(b"changed")

    def mutate_remove(target: Path, bundle: FrozenBundleIdentity) -> None:
        regular = next(
            entry for entry in bundle.entries
            if entry.kind == "file"
            and entry.relative_path != bundle.executable_relative
        )
        (target / regular.relative_path).unlink()

    def mutate_mode(target: Path, bundle: FrozenBundleIdentity) -> None:
        executable = target / bundle.executable_relative
        executable.chmod(
            stat.S_IMODE(executable.stat().st_mode) | stat.S_IWUSR,
        )

    for mutate in (
        mutate_add,
        mutate_change,
        mutate_remove,
        mutate_mode,
    ):
        with tempfile.TemporaryDirectory(prefix="materialized-tamper-") as raw:
            parent = Path(raw)
            bundle, _, _ = _capture(parent)
            run_dir = parent / "run"
            run_dir.mkdir(mode=0o700)
            destination = run_dir / "frozen-runner"
            materialize_frozen_bundle(bundle, destination)
            mutate(destination, bundle)
            assert not attest_materialized_frozen_bundle(
                bundle,
                destination,
            )
            try:
                materialize_frozen_bundle(bundle, destination)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("materialized tamper was accepted")


def main() -> None:
    test_complete_bundle_round_trip_and_materialization()
    test_macos_default_bundle_root_preserves_app_layout()
    test_artifact_workflow_installs_backend_relative_requirements()
    test_frozen_bundle_excludes_optional_mcp_cli_surface()
    test_windows_materialization_uses_acl_authority()
    test_artifact_smoke_failure_is_structured()
    test_materialization_normalizes_operational_exceptions()
    test_artifact_smoke_unexpected_failure_is_structured()
    test_private_diagnostic_log_rejects_redirects()
    test_private_diagnostic_log_preserves_exception_chain()
    test_source_add_change_remove_and_mode_tamper_are_rejected()
    test_materialized_add_change_remove_and_mode_tamper_are_rejected()
    print("frozen onedir artifact tests passed")


if __name__ == "__main__":
    main()
