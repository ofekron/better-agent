from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codex_execution_common import ExecutionContractError
from provider_claude_execution import (
    capture_embedded_claude_sdk,
    embedded_claude_sdk_attestation_failure,
)
from provider_frozen_bundle import (
    attest_materialized_frozen_bundle,
    remove_materialized_bundle,
)
from provider_launch_identity import capture_cli_launch
from provider_pinned_launch import open_pinned_runner_launch
from provider_runner_launch import RunnerLaunch, capture_runner_launch
from paths import ba_home
from private_diagnostics import append_private_exception


_RUNNERS = {
    "claude": ("runner", Path(__file__).with_name("runner.py")),
    "agy": ("runner_agy", Path(__file__).with_name("runner_agy.py")),
}


def _failure_type(exc: Exception) -> str:
    cause: BaseException = exc
    seen: set[int] = set()
    while cause.__cause__ is not None and id(cause) not in seen:
        seen.add(id(cause))
        cause = cause.__cause__
    return type(cause).__name__


def _safe_output_parent(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ExecutionContractError(f"artifact smoke {label} path is invalid")
    try:
        parent = path.parent.resolve(strict=True)
        observed = path.parent.lstat()
    except OSError as exc:
        raise ExecutionContractError(
            f"artifact smoke {label} directory is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
    ):
        raise ExecutionContractError(
            f"artifact smoke {label} directory is unsafe",
        )
    return parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-artifact-smoke", action="store_true")
    parser.add_argument("--artifact-probe", action="store_true")
    parser.add_argument("--family", choices=tuple(_RUNNERS))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _write_new_json(path: Path, payload: dict[str, Any], label: str) -> None:
    if path.exists():
        raise ExecutionContractError(f"artifact smoke {label} is invalid")
    parent = _safe_output_parent(path, label)
    descriptor = os.open(
        parent / path.name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short artifact smoke output write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    _write_new_json(path, payload, "output")


class _ProgressJournal:
    def __init__(self, output: Path) -> None:
        self._output = output
        self._sequence = 0

    def record(
        self,
        *,
        stage: str,
        status: str,
        elapsed_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "stage": stage,
            "status": status,
        }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        if error is not None:
            payload["error"] = error
        path = self._output.with_name(
            f"{self._output.stem}.progress.{self._sequence:04d}.json",
        )
        _write_new_json(path, payload, "progress checkpoint")
        self._sequence += 1


def _probe(family: str, output: Path) -> int:
    if not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS"):
        raise ExecutionContractError("artifact probe requires frozen runtime")
    runner_module, _ = _RUNNERS[family]
    loaded = {
        runner_module: str(
            Path(importlib.import_module(runner_module).__file__).resolve(),
        ),
    }
    if family == "claude":
        loaded["claude_agent_sdk"] = str(
            Path(importlib.import_module("claude_agent_sdk").__file__).resolve(),
        )
    _write_result(
        output,
        {
            "family": family,
            "frozen": True,
            "meipass": str(Path(sys._MEIPASS).resolve()),
            "modules": loaded,
        },
    )
    return 0


def _assert_windows_wrapper_rejected(root: Path) -> str:
    if os.name != "nt":
        return "not-applicable"
    wrapper = root / "provider.cmd"
    wrapper.write_bytes(b"@echo off\r\n")
    try:
        capture_cli_launch(
            logical_command="claude",
            launcher_path=wrapper,
            platform="win32",
            command_processor=sys.executable,
        )
    except ExecutionContractError:
        return "rejected"
    raise ExecutionContractError(
        "unsupported Windows command wrapper was accepted",
    )


def _tamper_materialized_sidecar(
    runner,
    materialized_root: Path,
) -> None:
    bundle = runner.frozen_bundle
    if bundle is None:
        raise ExecutionContractError("frozen bundle authority is missing")
    sidecar = Path(bundle.sidecar_relative)
    entry = next(
        (
            candidate for candidate in bundle.entries
            if candidate.kind == "file"
            and Path(candidate.relative_path).is_relative_to(sidecar)
            and candidate.relative_path != bundle.executable_relative
        ),
        None,
    )
    if entry is None:
        raise ExecutionContractError("frozen bundle sidecar is empty")
    target = materialized_root / entry.relative_path
    target.chmod(entry.mode | stat.S_IWUSR)
    with target.open("ab") as handle:
        handle.write(b"\nartifact-smoke-tamper")
        handle.flush()
        os.fsync(handle.fileno())
    if attest_materialized_frozen_bundle(bundle, materialized_root):
        raise ExecutionContractError("materialized sidecar tamper was accepted")
    # The materialized root is the shared fingerprint-keyed cache entry;
    # drop the now-tampered copy so later runs re-materialize a valid one.
    remove_materialized_bundle(materialized_root)


def _materialize_snapshot(
    root: Path,
    progress: _ProgressJournal,
) -> tuple[RunnerLaunch, Path, Path, dict[str, int]]:
    run_dir = root / "snapshot"
    progress.record(stage="snapshot-root", status="started")
    run_dir.mkdir(mode=0o700)
    progress.record(stage="snapshot-root", status="completed")
    started = time.perf_counter_ns()
    progress.record(stage="capture", status="started")
    runner = capture_runner_launch(
        run_dir=run_dir,
        executable_path=sys.executable,
        runner_entry=_RUNNERS["claude"][1],
        runner_kind="claude",
        runner_module="runner",
        frozen=True,
        platform=sys.platform,
    )
    if runner.frozen_bundle is None:
        raise ExecutionContractError("frozen runner authority is unavailable")
    embedded_sdk = capture_embedded_claude_sdk(runner)
    attestation_failure = embedded_claude_sdk_attestation_failure(
        embedded_sdk,
        runner,
    )
    if attestation_failure is not None:
        raise ExecutionContractError(
            "embedded Claude SDK authority mismatch: "
            + attestation_failure,
        )
    captured = time.perf_counter_ns()
    capture_ms = (captured - started) // 1_000_000
    progress.record(
        stage="capture",
        status="completed",
        elapsed_ms=capture_ms,
    )
    progress.record(stage="materialize", status="started")
    try:
        with open_pinned_runner_launch(runner) as pinned:
            materialized_executable = Path(pinned.argv[0])
    except Exception as exc:
        progress.record(
            stage="materialize",
            status="failed",
            error=f"materialization failed ({_failure_type(exc)})",
        )
        raise
    materialized = time.perf_counter_ns()
    materialize_ms = (materialized - captured) // 1_000_000
    progress.record(
        stage="materialize",
        status="completed",
        elapsed_ms=materialize_ms,
    )
    executable_depth = len(
        Path(runner.frozen_bundle.executable_relative).parts,
    )
    materialized_root = materialized_executable.parents[
        executable_depth - 1
    ]
    return (
        runner,
        materialized_executable,
        materialized_root,
        {
            "capture": capture_ms,
            "materialize": materialize_ms,
        },
    )


def _run_family_probe(
    family: str,
    root: Path,
    runner: RunnerLaunch,
    materialized_executable: Path,
    progress: _ProgressJournal,
) -> tuple[dict[str, Any], int]:
    runner_module, _ = _RUNNERS[family]
    probe_output = root / f"{family}-probe.json"
    environment = dict(os.environ)
    environment["BETTER_AGENT_HOME"] = str(root / "state")
    started = time.perf_counter_ns()
    progress.record(
        stage=f"probe:{family}",
        status="started",
    )
    completed = subprocess.run(
        [
            str(materialized_executable),
            "--frozen-artifact-smoke",
            "--artifact-probe",
            "--family",
            family,
            "--output",
            str(probe_output),
        ],
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        detail = ""
        try:
            failure = json.loads(probe_output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failure = None
        if type(failure) is dict and type(failure.get("error")) is str:
            detail = f": {failure['error']}"
        raise ExecutionContractError(
            f"{family} materialized artifact probe failed{detail}",
        )
    try:
        result = json.loads(probe_output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(
            f"{family} artifact probe output is invalid",
        ) from exc
    if (
        type(result) is not dict
        or result.get("family") != family
        or result.get("frozen") is not True
        or type(result.get("modules")) is not dict
        or runner_module not in result["modules"]
        or (
            family == "claude"
            and "claude_agent_sdk" not in result["modules"]
        )
    ):
        raise ExecutionContractError(
            f"{family} artifact probe result is incomplete",
        )
    assert runner.frozen_bundle is not None
    elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
    progress.record(
        stage=f"probe:{family}",
        status="completed",
        elapsed_ms=elapsed_ms,
    )
    return (
        {
            "bundle_fingerprint": runner.frozen_bundle.fingerprint,
            "embedded_sdk": family == "claude",
            "materialized_boot": True,
            "sidecar_tamper": "rejected",
        },
        elapsed_ms,
    )


def _smoke(output: Path, progress: _ProgressJournal) -> int:
    if not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS"):
        raise ExecutionContractError("artifact smoke requires frozen runtime")
    started = time.perf_counter_ns()
    progress.record(stage="state-root", status="started")
    state_root = ba_home()
    progress.record(stage="state-root", status="completed")
    progress.record(stage="temp-root", status="started")
    temporary = tempfile.TemporaryDirectory(
        prefix="better-agent-artifact-smoke-",
        dir=state_root,
    )
    progress.record(stage="temp-root", status="completed")
    try:
        raw = temporary.name
        root = Path(raw)
        runner, executable, materialized_root, timings = (
            _materialize_snapshot(root, progress)
        )
        probe_results = {
            family: _run_family_probe(
                family,
                root,
                runner,
                executable,
                progress,
            )
            for family in _RUNNERS
        }
        results = {
            family: result
            for family, (result, _) in probe_results.items()
        }
        timings["probes"] = sum(
            elapsed for _, elapsed in probe_results.values()
        )
        tamper_started = time.perf_counter_ns()
        progress.record(stage="tamper", status="started")
        _tamper_materialized_sidecar(runner, materialized_root)
        timings["tamper"] = (
            time.perf_counter_ns() - tamper_started
        ) // 1_000_000
        progress.record(
            stage="tamper",
            status="completed",
            elapsed_ms=timings["tamper"],
        )
        progress.record(
            stage="windows-wrapper",
            status="started",
        )
        wrapper = _assert_windows_wrapper_rejected(root)
        progress.record(
            stage="windows-wrapper",
            status="completed",
        )
    finally:
        progress.record(stage="temp-cleanup", status="started")
        temporary.cleanup()
        progress.record(stage="temp-cleanup", status="completed")
    timings["total"] = (
        time.perf_counter_ns() - started
    ) // 1_000_000
    _write_result(
        output,
        {
            "families": results,
            "platform": sys.platform,
            "timings_ms": timings,
            "windows_wrapper": wrapper,
        },
    )
    return 0


def _execute(
    args: argparse.Namespace,
    progress: _ProgressJournal,
) -> int:
    if not args.frozen_artifact_smoke:
        raise ExecutionContractError("artifact smoke mode is required")
    if args.artifact_probe:
        if args.family is None:
            raise ExecutionContractError("artifact probe family is required")
        return _probe(args.family, args.output)
    if args.family is not None:
        raise ExecutionContractError(
            "artifact smoke family is probe-only",
        )
    return _smoke(args.output, progress)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    progress = _ProgressJournal(args.output)
    try:
        progress.record(stage="smoke", status="started")
        result = _execute(args, progress)
        progress.record(stage="smoke", status="completed")
        return result
    except Exception as exc:
        try:
            append_private_exception(
                exc,
                context="artifact smoke failure",
            )
        except Exception:
            pass
        unexpected = not isinstance(exc, ExecutionContractError)
        error = str(exc) if not unexpected else "artifact smoke failed"
        exception_type = _failure_type(exc)
        has_cause = exc.__cause__ is not None
        progress.record(
            stage="smoke",
            status="failed",
            error=(
                f"{error} ({exception_type})"
                if unexpected or has_cause
                else error
            ),
        )
        payload = {"error": error}
        if unexpected or has_cause:
            payload["exception_type"] = exception_type
        _write_result(args.output, payload)
        return 1


__all__ = ["main"]
