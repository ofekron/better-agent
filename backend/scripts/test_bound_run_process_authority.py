#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

_test_home = tempfile.TemporaryDirectory(prefix="ba-bound-authority-")
os.environ["BETTER_AGENT_HOME"] = _test_home.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider import (  # noqa: E402
    BoundRunAuthority,
    _argv_path_belongs_to_run,
    _command_belongs_to_run,
    _process_argv_authority,
    classify_bound_run_authority,
)
import psutil  # noqa: E402


def _wait_for_process(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("authority fixture exited before census")
        if process.stdout and process.stdout.readline().strip() == "ready":
            return
    raise AssertionError("authority fixture did not become ready")


def test_path_membership_is_component_exact() -> None:
    assert _argv_path_belongs_to_run(
        "/tmp/runs/run-a/claude",
        runs_root="/tmp/runs",
        run_id="run-a",
        windows=False,
    )
    assert not _argv_path_belongs_to_run(
        "/tmp/runs/run-ab/claude",
        runs_root="/tmp/runs",
        run_id="run-a",
        windows=False,
    )
    assert _argv_path_belongs_to_run(
        r"C:\RUNS\Run-A\claude.exe",
        runs_root=r"c:\runs",
        run_id="RUN-A",
        windows=True,
    )
    assert not _argv_path_belongs_to_run(
        r"C:\runs\run-ab\claude.exe",
        runs_root=r"C:\runs",
        run_id="run-a",
        windows=True,
    )


def test_run_dir_flag_and_staged_cli_path_are_authoritative() -> None:
    assert _command_belongs_to_run(
        ["python", "runner.py", "--run-dir", "/tmp/runs/run-a"],
        runs_root="/tmp/runs",
        run_id="run-a",
        windows=False,
    )
    assert _command_belongs_to_run(
        ["node", "/tmp/runs/run-a/runtime/claude"],
        runs_root="/tmp/runs",
        run_id="run-a",
        windows=False,
    )


def test_real_process_census_finds_exact_run() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-runs-") as root:
        runs_root = Path(root)
        run_id = f"run-{uuid.uuid4()}"
        run_dir = runs_root / run_id
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
                "--run-dir",
                str(run_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_process(process)
            authority = _process_argv_authority(runs_root, run_id)
            assert authority.state == "owned", authority
            prefix = _process_argv_authority(runs_root, f"{run_id}-other")
            assert prefix.state != "owned", prefix
        finally:
            process.terminate()
            process.wait(timeout=5)


def test_real_process_census_finds_staged_cli_path() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-runs-") as root:
        runs_root = Path(root)
        run_id = f"run-{uuid.uuid4()}"
        run_dir = runs_root / run_id
        run_dir.mkdir()
        staged_cli = run_dir / "claude.py"
        staged_cli.write_text(
            "import time\nprint('ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, "-u", str(staged_cli)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_process(process)
            shutil.rmtree(run_dir)
            authority = _process_argv_authority(runs_root, run_id)
            assert authority.state == "owned", authority
        finally:
            process.terminate()
            process.wait(timeout=5)


class _Process:
    def __init__(
        self,
        result: object,
        *,
        created_at: float = 10,
        name: str = "python",
    ) -> None:
        self._result = result
        self._created_at = created_at
        self._name = name

    def create_time(self) -> float:
        return self._created_at

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result  # type: ignore[return-value]


def test_process_census_fails_closed_on_incomplete_inspection() -> None:
    root = Path("/tmp/runs")
    cases = [
        psutil.AccessDenied(pid=1),
        ["valid", 7],
    ]
    for result in cases:
        with patch("psutil.process_iter", return_value=iter([_Process(result)])):
            authority = _process_argv_authority(root, "run-a")
        assert authority.state == "unknown", (result, authority)
    with patch(
        "psutil.process_iter",
        return_value=iter([_Process(psutil.ZombieProcess(pid=1))]),
    ):
        assert _process_argv_authority(root, "run-a").state == "absent"
    with patch(
        "psutil.process_iter",
        side_effect=psutil.AccessDenied(pid=1),
    ):
        assert _process_argv_authority(root, "run-a").state == "unknown"
    with patch(
        "psutil.process_iter",
        return_value=iter(
            [
                _Process(psutil.NoSuchProcess(pid=1)),
                _Process([]),
            ]
        ),
    ):
        assert _process_argv_authority(root, "run-a").state == "absent"
    with patch(
        "psutil.process_iter",
        return_value=iter(
            [_Process(psutil.AccessDenied(pid=1), created_at=1)]
        ),
    ):
        assert _process_argv_authority(
            root,
            "run-a",
            started_after=2,
        ).state == "absent"
    with patch(
        "psutil.process_iter",
        return_value=iter(
            [_Process(psutil.AccessDenied(pid=1), name="softwareupdated")]
        ),
    ):
        assert _process_argv_authority(
            root,
            "run-a",
            process_name_prefixes=("python", "node", "claude"),
        ).state == "absent"
    with patch(
        "psutil.process_iter",
        return_value=iter(
            [_Process(psutil.AccessDenied(pid=1), name="Python")]
        ),
    ):
        assert _process_argv_authority(
            root,
            "run-a",
            process_name_prefixes=("python", "node", "claude"),
        ).state == "unknown"


class _Owner:
    supports_bound_run_argv_authority = True
    bound_run_process_name_prefixes = ("python", "node", "claude")

    def __init__(
        self,
        *,
        admitted: bool = False,
        defunct: bool = False,
        suspended: bool = False,
    ) -> None:
        self._admitted = admitted
        self.defunct = defunct
        self.suspended = suspended

    def has_admitted_run(self, run_id: str) -> bool:
        return self._admitted


class _Containment:
    def __init__(self, result: object) -> None:
        self._result = result

    def enumerate(self, run_id: str) -> list[int]:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result  # type: ignore[return-value]


def _classify(
    root: Path,
    *,
    owner: _Owner | None = None,
    record: dict | None = None,
    containment_result: object = (),
    process_result: BoundRunAuthority | None = None,
) -> BoundRunAuthority:
    owner = owner or _Owner()
    record = {"id": "provider-a", "kind": "claude"} if record is None else record
    process_result = process_result or BoundRunAuthority("absent", "test")
    with ExitStack() as stack:
        stack.enter_context(patch("config_store.get_provider", return_value=record))
        stack.enter_context(patch("provider.get_provider", return_value=owner))
        stack.enter_context(patch("runs_dir.runs_root", return_value=root))
        stack.enter_context(
            patch(
                "containment.containment",
                return_value=_Containment(containment_result),
            )
        )
        stack.enter_context(
            patch(
                "provider._process_argv_authority",
                return_value=process_result,
            )
        )
        return classify_bound_run_authority("provider-a", "run-a")


def test_classifier_fail_closed_and_owned_sources() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-classifier-") as root_value:
        root = Path(root_value)
        assert classify_bound_run_authority("remote:node", "run-a").state == "unknown"
        assert classify_bound_run_authority("provider-a", "../run-a").state == "unknown"
        with patch("config_store.get_provider", return_value=None), patch(
            "provider.get_provider",
            side_effect=AssertionError("deleted provider cache was consulted"),
        ):
            assert (
                classify_bound_run_authority("provider-a", "run-a").state
                == "unknown"
            )
        assert _classify(
            root,
            record={"id": "provider-a", "kind": "claude", "suspended": True},
        ).state == "unknown"
        assert _classify(root, owner=_Owner(defunct=True)).state == "unknown"
        assert _classify(root, owner=_Owner(suspended=True)).state == "unknown"
        unsupported = _Owner()
        unsupported.supports_bound_run_argv_authority = False
        assert _classify(root, owner=unsupported).state == "unknown"
        assert _classify(root, owner=_Owner(admitted=True)).state == "owned"
        assert _classify(root, containment_result=[123]).state == "owned"
        assert _classify(root, containment_result=RuntimeError()).state == "unknown"
        (root / "run-a").mkdir()
        assert _classify(root).state == "owned"


def test_classifier_rechecks_authority_after_process_census() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-classifier-") as root_value:
        root = Path(root_value)

        def reappear(
            _root: Path,
            _run_id: str,
            **_kwargs,
        ) -> BoundRunAuthority:
            (root / "run-a").mkdir()
            return BoundRunAuthority("absent", "test")

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "config_store.get_provider",
                    return_value={"id": "provider-a", "kind": "claude"},
                )
            )
            stack.enter_context(patch("provider.get_provider", return_value=_Owner()))
            stack.enter_context(patch("runs_dir.runs_root", return_value=root))
            stack.enter_context(
                patch(
                    "containment.containment",
                    return_value=_Containment(()),
                )
            )
            stack.enter_context(
                patch("provider._process_argv_authority", side_effect=reappear)
            )
            authority = classify_bound_run_authority("provider-a", "run-a")
        assert authority.state == "owned"


def main() -> None:
    tests = [
        test_path_membership_is_component_exact,
        test_run_dir_flag_and_staged_cli_path_are_authoritative,
        test_real_process_census_finds_exact_run,
        test_real_process_census_finds_staged_cli_path,
        test_process_census_fails_closed_on_incomplete_inspection,
        test_classifier_fail_closed_and_owned_sources,
        test_classifier_rechecks_authority_after_process_census,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} bound-run authority tests passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        _test_home.cleanup()
