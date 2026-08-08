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


def test_runtime_module_probe_has_a_generous_timeout() -> None:
    with patch.object(dependency_plan.subprocess, "run") as run:
        dependency_plan._probe_runtime_module(Path("python"), "slow_module")

    assert run.call_args.kwargs["timeout"] == dependency_plan.PROBE_TIMEOUT_SECONDS


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


def test_runtime_module_probe_timeout_is_trusted_not_rebuilt() -> None:
    """A stalled probe subprocess under load must not force a rebuild: the
    plan hash + marker already decided staleness, the probe is only a sanity
    check. This reproduces the RCA'd bug where a probe timeout raised
    DependencyPlanError and forced a full rebuild under load."""

    def probe(_python: Path, module: str) -> None:
        if module == "slow":
            raise subprocess.TimeoutExpired(cmd=["python", "-c", "import slow"], timeout=60)

    with patch.object(dependency_plan, "_probe_runtime_module", side_effect=probe):
        with patch.object(dependency_plan._logger, "warning") as warning:
            dependency_plan._probe_environment(
                Path("environment"),
                ("fast", "slow"),
            )

    assert warning.call_count == 1
    assert "slow" in warning.call_args.args[2]


def test_runtime_module_probe_timeout_does_not_mask_genuine_failure() -> None:
    """A genuine ImportError must still reject the environment even when a
    sibling probe merely timed out — only the timeout is trusted, not a real
    failure."""

    def probe(_python: Path, module: str) -> None:
        if module == "slow":
            raise subprocess.TimeoutExpired(cmd=["python", "-c", "import slow"], timeout=60)
        if module == "broken":
            raise subprocess.CalledProcessError(1, ["python"])

    with patch.object(dependency_plan, "_probe_runtime_module", side_effect=probe):
        try:
            dependency_plan._probe_environment(
                Path("environment"),
                ("slow", "broken"),
            )
        except dependency_plan.DependencyPlanError as exc:
            assert "broken" in str(exc)
            assert "slow" not in str(exc)
        else:
            raise AssertionError("genuine failure must still reject the environment")


if __name__ == "__main__":
    test_runtime_module_probes_run_in_parallel()
    test_windows_runtime_module_probes_run_serially()
    test_runtime_module_probe_has_a_generous_timeout()
    test_runtime_module_probe_failure_names_the_module()
    test_runtime_module_probe_timeout_is_trusted_not_rebuilt()
    test_runtime_module_probe_timeout_does_not_mask_genuine_failure()
    print("dependency probe parallel tests passed")
