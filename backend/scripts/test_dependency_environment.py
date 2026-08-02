from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import dependency_environment as de

VENV_ROOT = de.VENV_ROOT_NAME  # ".venvs"


def _make_active_venv(backend_dir: Path, relative: str = ".venvs/abc") -> Path:
    """Create a runnable venv layout under backend_dir and write the pointer.

    The pointer holds the env-DIR relative path (e.g. ".venvs/abc"); active_env
    appends bin/python to check runnability."""
    env_dir = backend_dir / relative
    (env_dir / "bin").mkdir(parents=True, exist_ok=True)
    (env_dir / "bin" / "python").write_text("#!/bin/sh\n")
    (backend_dir / de.ACTIVE_POINTER_NAME).write_text(relative, encoding="utf-8")
    return env_dir.resolve()


class TestPythonIn:
    def test_posix_layout(self) -> None:
        assert de.python_in(Path("/env")) == Path("/env/bin/python")


class TestActiveEnv:
    def test_happy_path_returns_resolved_env_dir(self, tmp_path) -> None:
        env_dir = _make_active_venv(tmp_path)
        assert de.active_env(tmp_path) == env_dir

    def test_missing_pointer_raises_not_activated(self, tmp_path) -> None:
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "not activated" in str(exc.value)

    def test_empty_pointer_raises_invalid(self, tmp_path) -> None:
        (tmp_path / de.ACTIVE_POINTER_NAME).write_text("   ", encoding="utf-8")
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "invalid" in str(exc.value)

    def test_absolute_pointer_raises_invalid(self, tmp_path) -> None:
        (tmp_path / de.ACTIVE_POINTER_NAME).write_text("/etc/passwd", encoding="utf-8")
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "invalid" in str(exc.value)

    def test_traversal_pointer_raises_invalid(self, tmp_path) -> None:
        (tmp_path / de.ACTIVE_POINTER_NAME).write_text("../escape", encoding="utf-8")
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "invalid" in str(exc.value)

    def test_pointer_outside_venv_root_raises_not_runnable(self, tmp_path) -> None:
        # a RUNNABLE env dir (bin/python present) but NOT under .venvs → rejected
        # because venv_root is not among env_dir.parents
        rogue = tmp_path / "rogue"
        (rogue / "bin").mkdir(parents=True)
        (rogue / "bin" / "python").write_text("")
        (tmp_path / de.ACTIVE_POINTER_NAME).write_text("rogue", encoding="utf-8")
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "not runnable" in str(exc.value)

    def test_pointer_under_venv_root_but_no_python_raises_not_runnable(
        self, tmp_path
    ) -> None:
        (tmp_path / VENV_ROOT / "abc").mkdir(parents=True)
        (tmp_path / de.ACTIVE_POINTER_NAME).write_text(
            f"{VENV_ROOT}/abc/bin/python", encoding="utf-8"
        )
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.active_env(tmp_path)
        assert "not runnable" in str(exc.value)


class TestVerifiedActiveEnv:
    def test_missing_planner_raises_no_planner(self, tmp_path, monkeypatch) -> None:
        _make_active_venv(tmp_path)  # active_env resolves, but no dependency_plan.py
        # subprocess must never run; fail the test if it does
        monkeypatch.setattr(de.subprocess, "run", lambda *a, **k: pytest.fail("subprocess ran"))
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.verified_active_env(tmp_path)
        assert "no dependency planner" in str(exc.value)

    def test_stale_subprocess_raises_stale(self, tmp_path, monkeypatch) -> None:
        _make_active_venv(tmp_path)
        (tmp_path / "dependency_plan.py").write_text("# planner")
        monkeypatch.setattr(
            de.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "py")),
        )
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.verified_active_env(tmp_path)
        assert "stale" in str(exc.value)

    def test_oserror_during_subprocess_raises_stale(self, tmp_path, monkeypatch) -> None:
        _make_active_venv(tmp_path)
        (tmp_path / "dependency_plan.py").write_text("# planner")
        monkeypatch.setattr(
            de.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        with pytest.raises(de.DependencyEnvironmentError) as exc:
            de.verified_active_env(tmp_path)
        assert "stale" in str(exc.value)

    def test_successful_subprocess_returns_env_dir(self, tmp_path, monkeypatch) -> None:
        env_dir = _make_active_venv(tmp_path)
        (tmp_path / "dependency_plan.py").write_text("# planner")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(de.subprocess, "run", fake_run)
        result = de.verified_active_env(tmp_path)
        assert result == env_dir
        # invokes python + planner assert-active-plan from the backend dir
        assert str(de.python_in(env_dir)) in str(captured["argv"][0])
        assert "assert-active-plan" in captured["argv"]
        assert str(tmp_path) == str(captured["cwd"])

    def test_subprocess_env_strips_pythonhome_and_pythonpath(
        self, tmp_path, monkeypatch
    ) -> None:
        _make_active_venv(tmp_path)
        (tmp_path / "dependency_plan.py").write_text("# planner")
        monkeypatch.setenv("PYTHONHOME", "/should/be/stripped")
        monkeypatch.setenv("PYTHONPATH", "/should/be/stripped")
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(de.subprocess, "run", fake_run)
        de.verified_active_env(tmp_path)
        env = captured["env"]
        assert "PYTHONHOME" not in env
        assert "PYTHONPATH" not in env


class TestVerifiedActivePython:
    def test_returns_python_inside_verified_env(self, tmp_path, monkeypatch) -> None:
        env_dir = _make_active_venv(tmp_path)
        (tmp_path / "dependency_plan.py").write_text("# planner")
        monkeypatch.setattr(
            de.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0),
        )
        assert de.verified_active_python(tmp_path) == de.python_in(env_dir)
