from __future__ import annotations

import importlib.util
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from codex_execution_common import (
    ExecutionContractError,
    required_string,
    timed_contract_step,
)
from codex_execution_identity import (
    FileIdentity,
    file_identity_from_dict,
    file_identity_to_dict,
)
from provider_frozen_bundle import FrozenBundleIdentity
from provider_launch_identity import (
    AttestedLaunch,
    _SAFE_NAME_RE,
    _require_object,
    _validate_launch,
)


@dataclass(frozen=True)
class RunnerLaunch:
    launch: AttestedLaunch
    runner_entry: FileIdentity | None
    development_runtime: FrozenBundleIdentity | None
    frozen_bundle: FrozenBundleIdentity | None
    runner_kind: str
    runner_module: str
    frozen: bool

    def validate(self) -> None:
        _validate_runner(self)
        _validate_launch(self.launch)
        if self.frozen_bundle is not None:
            self.frozen_bundle.validate()
        if self.development_runtime is not None:
            self.development_runtime.validate()

    def attest(self) -> bool:
        with timed_contract_step("provider.runner_launch.attest"):
            return self._attest()

    def _attest(self) -> bool:
        try:
            self.validate()
        except ExecutionContractError:
            return False
        if self.frozen:
            return (
                self.frozen_bundle is not None
                and self.frozen_bundle.attest()
            )
        return (
            self.launch.attest()
            and (
                self.runner_entry is None
                or self.runner_entry.attest()
            )
            and (
                self.development_runtime is None
                or self.development_runtime.attest()
            )
            and (
                self.frozen_bundle is None
                or self.frozen_bundle.attest()
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "launch": self.launch.to_dict(),
            "runner_entry": (
                file_identity_to_dict(self.runner_entry)
                if self.runner_entry is not None
                else None
            ),
            "development_runtime": (
                self.development_runtime.to_dict()
                if self.development_runtime is not None
                else None
            ),
            "frozen_bundle": (
                self.frozen_bundle.to_dict()
                if self.frozen_bundle is not None
                else None
            ),
            "runner_kind": self.runner_kind,
            "runner_module": self.runner_module,
            "frozen": self.frozen,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RunnerLaunch:
        _require_object(raw, {
            "launch",
            "runner_entry",
            "development_runtime",
            "frozen_bundle",
            "runner_kind",
            "runner_module",
            "frozen",
        }, "runner launch")
        if (
            type(raw["launch"]) is not dict
            or (
                raw["runner_entry"] is not None
                and type(raw["runner_entry"]) is not dict
            )
            or (
                raw["development_runtime"] is not None
                and type(raw["development_runtime"]) is not dict
            )
            or (
                raw["frozen_bundle"] is not None
                and type(raw["frozen_bundle"]) is not dict
            )
            or type(raw["frozen"]) is not bool
        ):
            raise ExecutionContractError("invalid runner launch")
        value = cls(
            launch=AttestedLaunch.from_dict(raw["launch"]),
            runner_entry=(
                file_identity_from_dict(raw["runner_entry"])
                if raw["runner_entry"] is not None
                else None
            ),
            development_runtime=(
                FrozenBundleIdentity.from_dict(raw["development_runtime"])
                if raw["development_runtime"] is not None
                else None
            ),
            frozen_bundle=(
                FrozenBundleIdentity.from_dict(raw["frozen_bundle"])
                if raw["frozen_bundle"] is not None
                else None
            ),
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


def _same_resolved_file(
    left: FileIdentity | None,
    right: FileIdentity,
) -> bool:
    return (
        left is not None
        and left.resolved_path == right.resolved_path
        and left.sha256 == right.sha256
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.ctime_ns == right.ctime_ns
        and left.device == right.device
        and left.inode == right.inode
    )


def _package_parent(package_name: str) -> Path:
    spec = importlib.util.find_spec(package_name)
    locations = tuple(spec.submodule_search_locations or ()) if spec else ()
    if len(locations) != 1:
        raise ExecutionContractError(
            f"{package_name} package root is unavailable",
        )
    return Path(locations[0]).resolve(strict=True).parent


def _development_runtime(
    executable: FileIdentity,
) -> FrozenBundleIdentity | None:
    try:
        current = Path(sys.executable).resolve(strict=True)
    except OSError:
        return None
    if Path(executable.resolved_path) != current:
        return None
    prefix_root = Path(sys.prefix).resolve(strict=True)
    base_root = Path(sys.base_prefix).resolve(strict=True)
    if prefix_root == base_root:
        return None
    stdlib_root = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    # The venv's own prefix directory doesn't always contain the resolved
    # interpreter and its stdlib/shared-library dependencies: tools like
    # `uv venv` make bin/python a symlink out to a centrally-managed
    # interpreter store rooted at base_prefix instead of copying the
    # interpreter into the venv. Bundle from whichever resolved root
    # actually contains both, so the pinned copy brings its dylib along.
    if current.is_relative_to(prefix_root) and stdlib_root.is_relative_to(
        prefix_root,
    ):
        runtime_root = prefix_root
    elif current.is_relative_to(base_root) and stdlib_root.is_relative_to(
        base_root,
    ):
        if current.is_relative_to(Path("/bin")) or current.is_relative_to(
            Path("/usr/bin"),
        ):
            return None
        runtime_root = base_root
    else:
        return None
    site_packages = stdlib_root / "site-packages"
    excluded = (
        (
            site_packages.relative_to(runtime_root).as_posix(),
        )
        if site_packages.exists() or site_packages.is_symlink()
        else ()
    )
    try:
        return FrozenBundleIdentity.capture(
            executable_path=executable.resolved_path,
            bundle_root=runtime_root,
            sidecar_root=stdlib_root,
            excluded_relative_paths=excluded,
        )
    except (ExecutionContractError, OSError):
        # Best-effort dev-runtime fingerprint — a stale interpreter/env
        # reference (e.g. a pruned conda pkgs-cache symlink target) must not
        # fail the whole turn; every caller already treats None as skip.
        return None


def _validate_runner(value: RunnerLaunch) -> None:
    argv = value.launch.argv
    if (
        not _SAFE_NAME_RE.fullmatch(value.runner_kind)
        or not _SAFE_NAME_RE.fullmatch(value.runner_module)
        or value.launch.mode
        != ("runner-frozen" if value.frozen else "runner-dev")
    ):
        raise ExecutionContractError("incoherent runner launch")
    if value.frozen:
        bundle = value.frozen_bundle
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
                "--runner-module",
                value.runner_module,
            )
        )
        if (
            value.runner_entry is not None
            or value.development_runtime is not None
            or bundle is None
            or argv != expected
            or not _is_absolute_path(argv[2])
            or (
                Path(bundle.root.resolved_path)
                / bundle.executable_relative
            ) != Path(value.launch.components[0].resolved_path)
            or not _same_resolved_file(
                next(
                    (
                        entry.file
                        for entry in bundle.entries
                        if entry.relative_path
                        == bundle.executable_relative
                    ),
                    None,
                ),
                value.launch.components[0],
            )
        ):
            raise ExecutionContractError("incoherent runner launch")
        return
    if (
        value.runner_entry is None
        or value.frozen_bundle is not None
        or Path(value.runner_entry.resolved_path).stem != value.runner_module
    ):
        raise ExecutionContractError("incoherent runner launch")
    runtime = value.development_runtime
    if runtime is not None and (
        (
            Path(runtime.root.resolved_path)
            / runtime.executable_relative
        ) != Path(value.launch.components[0].resolved_path)
        or not _same_resolved_file(
            next(
                (
                    entry.file
                    for entry in runtime.entries
                    if entry.relative_path == runtime.executable_relative
                ),
                None,
            ),
            value.launch.components[0],
        )
    ):
        raise ExecutionContractError("incoherent runner launch")
    if value.runner_kind == "claude" and value.runner_module == "runner":
        expected = (
            value.launch.components[0].resolved_path,
            "-I",
            value.runner_entry.resolved_path,
            "--run-dir",
            argv[4] if len(argv) >= 5 else "",
            "--backend-root",
            str(Path(value.runner_entry.resolved_path).parent),
            "--site-packages-root",
            argv[8] if len(argv) >= 9 else "",
            "--application-sdk-root",
            argv[10] if len(argv) >= 11 else "",
        )
        expected_indexes = (0, 2)
        run_dir_index = 4
    else:
        expected = (
            value.launch.components[0].resolved_path,
            value.runner_entry.resolved_path,
            "--run-dir",
            argv[3] if len(argv) >= 4 else "",
        )
        expected_indexes = (0, 1)
        run_dir_index = 3
    if (
        argv != expected
        or value.launch.components[1] != value.runner_entry
        or value.launch.component_argv_indexes != expected_indexes
        or not _is_absolute_path(argv[run_dir_index])
        or (
            value.runner_kind == "claude"
            and value.runner_module == "runner"
            and (
                not _is_absolute_path(argv[8])
                or not _is_absolute_path(argv[10])
            )
        )
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
    frozen_bundle_root: str | Path | None = None,
    frozen_sidecar_root: str | Path | None = None,
) -> RunnerLaunch:
    with timed_contract_step("provider.runner_launch.capture"):
        return _capture_runner_launch(
            run_dir=run_dir,
            executable_path=executable_path,
            runner_entry=runner_entry,
            runner_kind=runner_kind,
            runner_module=runner_module,
            frozen=frozen,
            platform=platform,
            frozen_bundle_root=frozen_bundle_root,
            frozen_sidecar_root=frozen_sidecar_root,
        )


def _capture_runner_launch(
    *,
    run_dir: str | Path,
    executable_path: str | Path,
    runner_entry: str | Path,
    runner_kind: str,
    runner_module: str,
    frozen: bool,
    platform: str | None = None,
    frozen_bundle_root: str | Path | None = None,
    frozen_sidecar_root: str | Path | None = None,
) -> RunnerLaunch:
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        raise ExecutionContractError("run directory must be absolute")
    executable = FileIdentity.capture(executable_path)
    if frozen:
        entry = None
        development_runtime = None
        frozen_bundle = FrozenBundleIdentity.capture(
            executable_path=executable.resolved_path,
            bundle_root=frozen_bundle_root,
            sidecar_root=frozen_sidecar_root,
        )
        argv = [
            executable.resolved_path,
            "--run-dir",
            str(run_path.absolute()),
        ]
        if runner_module != "runner":
            # `--runner-module` is passed explicitly (not re-derived from
            # `--runner-kind` alone) because "claude" now maps to two
            # different modules depending on the configured runner
            # ("runner" native, "runner_better_agent" subscription-via-
            # Better-Agent-runner) — app_entry.py must not guess.
            argv.extend(("--runner-kind", runner_kind, "--runner-module", runner_module))
        components = (executable,)
        indexes = (0,)
        mode = "runner-frozen"
    else:
        entry = FileIdentity.capture(runner_entry)
        development_runtime = _development_runtime(executable)
        frozen_bundle = None
    if not frozen and runner_kind == "claude" and runner_module == "runner":
        site_packages_root = Path(
            sysconfig.get_path("purelib"),
        ).resolve(strict=True)
        application_sdk_root = _package_parent("better_agent_sdk")
        argv = [
            executable.resolved_path,
            "-I",
            entry.resolved_path,
            "--run-dir",
            str(run_path.absolute()),
            "--backend-root",
            str(Path(entry.resolved_path).parent),
            "--site-packages-root",
            str(site_packages_root),
            "--application-sdk-root",
            str(application_sdk_root),
        ]
        components = (executable, entry)
        indexes = (0, 2)
        mode = "runner-dev"
    elif not frozen:
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
        development_runtime=development_runtime,
        frozen_bundle=frozen_bundle,
        runner_kind=runner_kind,
        runner_module=runner_module,
        frozen=frozen,
    )
    _validate_runner(value)
    return value


def retarget_runner_launch(
    runner: RunnerLaunch,
    run_dir: str | Path,
) -> RunnerLaunch:
    with timed_contract_step("provider.runner_launch.retarget"):
        return _retarget_runner_launch(runner, run_dir)


def _retarget_runner_launch(
    runner: RunnerLaunch,
    run_dir: str | Path,
) -> RunnerLaunch:
    if not isinstance(runner, RunnerLaunch) or not runner.attest():
        raise ExecutionContractError("runner launch authority is unavailable")
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        raise ExecutionContractError("run directory must be absolute")
    run_dir_index = (
        2
        if runner.frozen
        else 4
        if runner.runner_kind == "claude" and runner.runner_module == "runner"
        else 3
    )
    argv = list(runner.launch.argv)
    if (
        len(argv) <= run_dir_index
        or argv[run_dir_index - 1] != "--run-dir"
    ):
        raise ExecutionContractError("incoherent runner launch")
    argv[run_dir_index] = str(run_path.absolute())
    launch = AttestedLaunch(
        logical_command=runner.launch.logical_command,
        platform=runner.launch.platform,
        mode=runner.launch.mode,
        argv=tuple(argv),
        launcher=runner.launch.launcher,
        components=runner.launch.components,
        component_argv_indexes=runner.launch.component_argv_indexes,
    )
    _validate_launch(launch)
    retargeted = RunnerLaunch(
        launch=launch,
        runner_entry=runner.runner_entry,
        development_runtime=runner.development_runtime,
        frozen_bundle=runner.frozen_bundle,
        runner_kind=runner.runner_kind,
        runner_module=runner.runner_module,
        frozen=runner.frozen,
    )
    _validate_runner(retargeted)
    if not retargeted.attest():
        raise ExecutionContractError("runner launch authority changed")
    return retargeted


__all__ = [
    "RunnerLaunch",
    "capture_runner_launch",
    "retarget_runner_launch",
]
