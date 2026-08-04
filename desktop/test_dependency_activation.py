from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dependency_activation


def _checkout(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    backend.mkdir(parents=True)
    (backend / "main.py").write_text("", encoding="utf-8")
    (backend / "dependency_plan.py").write_text("", encoding="utf-8")
    return tmp_path


def _uv(tmp_path: Path) -> Path:
    path = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    path.write_text("", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_activation_uses_sanitized_argv_and_strict_verified_result(
    tmp_path,
    monkeypatch,
) -> None:
    checkout = _checkout(tmp_path / "checkout")
    uv = _uv(tmp_path)
    env_dir = checkout / "backend" / ".venvs" / "active"
    captured: dict = {}

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ignored")

    monkeypatch.setattr(dependency_activation.subprocess, "run", run)
    def verified(backend, *, source_env):
        captured["verified_backend"] = backend
        captured["verified_env"] = source_env
        return env_dir

    monkeypatch.setattr(dependency_activation, "verified_active_env", verified)

    python = dependency_activation.activate_checkout(
        checkout,
        str(uv),
        source_env={
            "PATH": os.environ.get("PATH", ""),
            "BETTER_AGENT_HOME": str(tmp_path / "state"),
            "PYTHONPATH": "/poison",
            "PYTHONHOME": "/poison",
            "BETTER_AGENT_CREDENTIAL_SESSION_AUTH": "secret",
            "BETTER_AGENT_BACKEND_LAUNCH_TOKEN": "secret",
            "BA_PASSWORD": "secret",
        },
    )

    assert python == dependency_activation.python_in(env_dir)
    assert captured["argv"] == [
        str(Path(sys.executable).resolve()),
        str((checkout / "backend" / "dependency_plan.py").resolve()),
        "activate",
        "--uv",
        str(uv.resolve()),
    ]
    assert captured["kwargs"]["cwd"] == checkout / "backend"
    assert captured["kwargs"]["env"] == {
        "PATH": os.environ.get("PATH", ""),
        "BETTER_AGENT_HOME": str(tmp_path / "state"),
    }
    assert captured["verified_backend"] == checkout / "backend"
    assert captured["verified_env"] == captured["kwargs"]["env"]
    assert captured["kwargs"]["check"] is True


def test_activation_failure_does_not_run_strict_verification(tmp_path, monkeypatch) -> None:
    checkout = _checkout(tmp_path / "checkout")
    uv = _uv(tmp_path)
    monkeypatch.setattr(
        dependency_activation.subprocess,
        "run",
        lambda argv, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, argv)
        ),
    )
    monkeypatch.setattr(
        dependency_activation,
        "verified_active_env",
        lambda _backend, **_kwargs: pytest.fail("strict verification ran"),
    )

    with pytest.raises(dependency_activation.DependencyActivationError) as exc:
        dependency_activation.activate_checkout(checkout, str(uv))
    assert "activation failed" in str(exc.value)


def test_strict_post_activation_failure_propagates(tmp_path, monkeypatch) -> None:
    checkout = _checkout(tmp_path / "checkout")
    uv = _uv(tmp_path)
    monkeypatch.setattr(
        dependency_activation.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.setattr(
        dependency_activation,
        "verified_active_env",
        lambda _backend, **_kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    with pytest.raises(RuntimeError, match="stale"):
        dependency_activation.activate_checkout(checkout, str(uv))


@pytest.mark.parametrize("uv", ("missing-uv", "/missing/uv"))
def test_missing_uv_fails_before_activation(tmp_path, monkeypatch, uv: str) -> None:
    checkout = _checkout(tmp_path / "checkout")
    monkeypatch.setattr(dependency_activation.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dependency_activation.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("activation subprocess ran"),
    )

    with pytest.raises(dependency_activation.DependencyActivationError) as exc:
        dependency_activation.activate_checkout(checkout, uv)
    assert "uv executable is unavailable" in str(exc.value)
