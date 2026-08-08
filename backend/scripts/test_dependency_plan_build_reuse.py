from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import dependency_plan


def _plan(plan_hash: str = "abc123") -> dict:
    # probes=() keeps `_assert_environment` marker-only: no subprocess spawn,
    # so these tests never need a real python executable on disk.
    return {"hash": plan_hash, "requirements": (), "probes": ()}


def _write_marker(env_dir: Path, plan_hash: str) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / dependency_plan.PLAN_MARKER).write_text(
        json.dumps({"schema_version": 1, "hash": plan_hash}), encoding="utf-8"
    )


@pytest.fixture
def isolated_venv_root(tmp_path, monkeypatch):
    # `VENV_ROOT` is a module-level constant tied to the real backend
    # checkout, not to `bc_home()` — `paths.engage_test_home` does not
    # isolate it. Point it at a tempdir directly for the scope of the test.
    root = tmp_path / ".venvs"
    monkeypatch.setattr(dependency_plan, "VENV_ROOT", root)
    return root


def test_build_environment_reuses_existing_valid_sibling(isolated_venv_root) -> None:
    plan = _plan()
    plan_root = isolated_venv_root / plan["hash"]
    existing = plan_root / "existing-build"
    _write_marker(existing, plan["hash"])

    with patch.object(dependency_plan.subprocess, "run") as run:
        result = dependency_plan._build_environment("uv", plan)

    run.assert_not_called()
    assert result == existing


def test_build_environment_ignores_stage_directories(isolated_venv_root) -> None:
    plan = _plan()
    plan_root = isolated_venv_root / plan["hash"]
    # A leftover `.stage-*` dir from a crashed build is not a completed
    # build and must never be reused.
    stage = plan_root / ".stage-deadbeef"
    _write_marker(stage, plan["hash"])

    reusable = dependency_plan._reusable_build(plan_root, plan)

    assert reusable is None


def test_build_environment_ignores_mismatched_plan_hash(isolated_venv_root) -> None:
    plan = _plan("current-hash")
    plan_root = isolated_venv_root / plan["hash"]
    stale = plan_root / "stale-build"
    _write_marker(stale, "other-hash")

    reusable = dependency_plan._reusable_build(plan_root, plan)

    assert reusable is None


def test_build_environment_rebuilds_when_no_reusable_sibling(isolated_venv_root) -> None:
    plan = _plan()
    plan_root = isolated_venv_root / plan["hash"]
    stale = plan_root / "stale-build"
    _write_marker(stale, "other-hash")

    def fake_run(*args, **kwargs):
        # `_build_environment` shells out to `uv venv` / `uv pip install`
        # then writes the marker itself; simulate the marker write it does
        # by hand so `_probe_environment`/`_assert_environment` succeed.
        return None

    stage_dirs: list[Path] = []
    real_venv_command = dependency_plan._venv_creation_command

    def spy_venv_command(uv, target):
        stage_dirs.append(target)
        target.mkdir(parents=True, exist_ok=True)
        return real_venv_command(uv, target)

    with patch.object(dependency_plan, "_venv_creation_command", side_effect=spy_venv_command):
        with patch.object(dependency_plan.subprocess, "run", side_effect=fake_run):
            result = dependency_plan._build_environment("uv", plan)

    assert len(stage_dirs) == 1
    assert result.parent == plan_root
    assert result != stale
    marker = json.loads((result / dependency_plan.PLAN_MARKER).read_text(encoding="utf-8"))
    assert marker == {"schema_version": 1, "hash": plan["hash"]}


def test_reusable_build_returns_none_for_missing_plan_root(isolated_venv_root) -> None:
    plan = _plan()
    plan_root = isolated_venv_root / plan["hash"]

    assert dependency_plan._reusable_build(plan_root, plan) is None


if __name__ == "__main__":
    print(
        "run via pytest: "
        "./scripts/run-backend-tests.sh -- backend/scripts/test_dependency_plan_build_reuse.py -q"
    )
