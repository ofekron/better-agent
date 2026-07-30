from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import dependency_plan


def test_runtime_module_probes_run_in_parallel() -> None:
    barrier = threading.Barrier(2, timeout=2)
    observed: list[str] = []

    def probe(_python: Path, module: str) -> None:
        observed.append(module)
        barrier.wait()

    with patch.object(dependency_plan, "_probe_runtime_module", side_effect=probe):
        dependency_plan._probe_environment(Path("environment"), ("slow_a", "slow_b"))

    assert sorted(observed) == ["slow_a", "slow_b"]


def test_windows_runtime_module_probes_run_serially() -> None:
    assert dependency_plan._runtime_probe_workers(8, platform="nt") == 1


def test_runtime_module_probe_waits_for_process_completion() -> None:
    with patch.object(dependency_plan.subprocess, "run") as run:
        dependency_plan._probe_runtime_module(Path("python"), "slow_module")

    assert "timeout" not in run.call_args.kwargs


def test_runtime_module_probe_failure_names_the_module() -> None:
    def probe(_python: Path, module: str) -> None:
        if module == "broken":
            raise subprocess.CalledProcessError(1, ["python"])

    with patch.object(dependency_plan, "_probe_runtime_module", side_effect=probe):
        try:
            dependency_plan._probe_environment(
                Path("environment"),
                ("available", "broken"),
            )
        except dependency_plan.DependencyPlanError as exc:
            assert str(exc).endswith("broken")
        else:
            raise AssertionError("failed runtime import must reject the environment")


if __name__ == "__main__":
    test_runtime_module_probes_run_in_parallel()
    test_windows_runtime_module_probes_run_serially()
    test_runtime_module_probe_waits_for_process_completion()
    test_runtime_module_probe_failure_names_the_module()
    print("dependency probe parallel tests passed")
