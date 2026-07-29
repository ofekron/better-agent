from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from codex_execution_common import ExecutionContractError
from provider_claude_execution import (
    attest_embedded_claude_sdk,
    capture_embedded_claude_sdk,
)
from provider_frozen_bundle import attest_materialized_frozen_bundle
from provider_launch_identity import capture_cli_launch
from provider_pinned_launch import open_pinned_runner_launch
from provider_runner_launch import capture_runner_launch


_RUNNERS = {
    "claude": ("runner", Path(__file__).with_name("runner.py")),
    "agy": ("runner_agy", Path(__file__).with_name("runner_agy.py")),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-artifact-smoke", action="store_true")
    parser.add_argument("--artifact-probe", action="store_true")
    parser.add_argument("--family", choices=tuple(_RUNNERS))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    if (
        not path.is_absolute()
        or path.exists()
        or path.is_symlink()
    ):
        raise ExecutionContractError("artifact smoke output is invalid")
    try:
        parent = path.parent.resolve(strict=True)
        observed = path.parent.lstat()
    except OSError as exc:
        raise ExecutionContractError(
            "artifact smoke output directory is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
    ):
        raise ExecutionContractError(
            "artifact smoke output directory is unsafe",
        )
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


def _run_family_probe(
    family: str,
    root: Path,
) -> dict[str, Any]:
    runner_module, runner_entry = _RUNNERS[family]
    run_dir = root / family
    run_dir.mkdir(mode=0o700)
    runner = capture_runner_launch(
        run_dir=run_dir,
        executable_path=sys.executable,
        runner_entry=runner_entry,
        runner_kind=family,
        runner_module=runner_module,
        frozen=True,
        platform=sys.platform,
    )
    if runner.frozen_bundle is None or not runner.attest():
        raise ExecutionContractError("frozen runner authority is unavailable")
    embedded_sdk = None
    if family == "claude":
        embedded_sdk = capture_embedded_claude_sdk(runner)
        if not attest_embedded_claude_sdk(embedded_sdk, runner):
            raise ExecutionContractError(
                "embedded Claude SDK authority mismatch",
            )
    with open_pinned_runner_launch(runner) as pinned:
        materialized_executable = Path(pinned.argv[0])
    materialized_root = (
        run_dir / "frozen-runner"
    ).resolve(strict=True)
    probe_output = root / f"{family}-probe.json"
    environment = dict(os.environ)
    environment["BETTER_AGENT_HOME"] = str(root / "state")
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
        raise ExecutionContractError(
            f"{family} materialized artifact probe failed",
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
    _tamper_materialized_sidecar(runner, materialized_root)
    return {
        "bundle_fingerprint": runner.frozen_bundle.fingerprint,
        "embedded_sdk": embedded_sdk is not None,
        "materialized_boot": True,
        "sidecar_tamper": "rejected",
    }


def _smoke(output: Path) -> int:
    if not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS"):
        raise ExecutionContractError("artifact smoke requires frozen runtime")
    with tempfile.TemporaryDirectory(
        prefix="better-agent-artifact-smoke-",
    ) as raw:
        root = Path(raw)
        results = {
            family: _run_family_probe(family, root)
            for family in _RUNNERS
        }
        wrapper = _assert_windows_wrapper_rejected(root)
    _write_result(
        output,
        {
            "families": results,
            "platform": sys.platform,
            "windows_wrapper": wrapper,
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
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
    return _smoke(args.output)


__all__ = ["main"]
