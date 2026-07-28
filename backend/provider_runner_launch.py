from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError, required_string
from codex_execution_identity import FileIdentity, file_identity_from_dict
from provider_launch_identity import (
    AttestedLaunch,
    _SAFE_NAME_RE,
    _file_to_dict,
    _require_object,
    _validate_launch,
)


@dataclass(frozen=True)
class RunnerLaunch:
    launch: AttestedLaunch
    runner_entry: FileIdentity
    runner_kind: str
    runner_module: str
    frozen: bool

    def attest(self) -> bool:
        return self.launch.attest() and self.runner_entry.attest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "launch": self.launch.to_dict(),
            "runner_entry": _file_to_dict(self.runner_entry),
            "runner_kind": self.runner_kind,
            "runner_module": self.runner_module,
            "frozen": self.frozen,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RunnerLaunch:
        _require_object(raw, {
            "launch",
            "runner_entry",
            "runner_kind",
            "runner_module",
            "frozen",
        }, "runner launch")
        if (
            type(raw["launch"]) is not dict
            or type(raw["runner_entry"]) is not dict
            or type(raw["frozen"]) is not bool
        ):
            raise ExecutionContractError("invalid runner launch")
        value = cls(
            launch=AttestedLaunch.from_dict(raw["launch"]),
            runner_entry=file_identity_from_dict(raw["runner_entry"]),
            runner_kind=required_string(raw, "runner_kind"),
            runner_module=required_string(raw, "runner_module"),
            frozen=raw["frozen"],
        )
        _validate_runner(value)
        return value


def _is_absolute_path(value: str) -> bool:
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    )


def _validate_runner(value: RunnerLaunch) -> None:
    argv = value.launch.argv
    if (
        not _SAFE_NAME_RE.fullmatch(value.runner_kind)
        or not _SAFE_NAME_RE.fullmatch(value.runner_module)
        or value.launch.mode
        != ("runner-frozen" if value.frozen else "runner-dev")
        or Path(value.runner_entry.resolved_path).stem != value.runner_module
    ):
        raise ExecutionContractError("incoherent runner launch")
    if value.frozen:
        expected = (
            (
                value.launch.components[0].resolved_path,
                "--run-dir",
                argv[2] if len(argv) >= 3 else "",
            )
            if value.runner_module == "runner"
            else (
                value.launch.components[0].resolved_path,
                "--run-dir",
                argv[2] if len(argv) >= 3 else "",
                "--runner-kind",
                value.runner_kind,
            )
        )
        if (
            argv != expected
            or not _is_absolute_path(argv[2])
        ):
            raise ExecutionContractError("incoherent runner launch")
        return
    if (
        argv != (
            value.launch.components[0].resolved_path,
            value.runner_entry.resolved_path,
            "--run-dir",
            argv[3] if len(argv) >= 4 else "",
        )
        or value.launch.components[1] != value.runner_entry
        or not _is_absolute_path(argv[3])
    ):
        raise ExecutionContractError("incoherent runner launch")


def capture_runner_launch(
    *,
    run_dir: str | Path,
    executable_path: str | Path,
    runner_entry: str | Path,
    runner_kind: str,
    runner_module: str,
    frozen: bool,
    platform: str | None = None,
) -> RunnerLaunch:
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        raise ExecutionContractError("run directory must be absolute")
    executable = FileIdentity.capture(executable_path)
    entry = FileIdentity.capture(runner_entry)
    if frozen:
        argv = [
            executable.resolved_path,
            "--run-dir",
            str(run_path.absolute()),
        ]
        if runner_module != "runner":
            argv.extend(("--runner-kind", runner_kind))
        components = (executable,)
        indexes = (0,)
        mode = "runner-frozen"
    else:
        argv = [
            executable.resolved_path,
            entry.resolved_path,
            "--run-dir",
            str(run_path.absolute()),
        ]
        components = (executable, entry)
        indexes = (0, 1)
        mode = "runner-dev"
    launch = AttestedLaunch(
        logical_command=f"better-agent-{runner_kind}-runner",
        platform=platform or sys.platform,
        mode=mode,
        argv=tuple(argv),
        launcher=executable,
        components=components,
        component_argv_indexes=indexes,
    )
    _validate_launch(launch)
    value = RunnerLaunch(
        launch=launch,
        runner_entry=entry,
        runner_kind=runner_kind,
        runner_module=runner_module,
        frozen=frozen,
    )
    _validate_runner(value)
    return value
