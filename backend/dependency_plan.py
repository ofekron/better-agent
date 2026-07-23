#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent

import installation_profile
import provider_manifest
from paths import bc_home

BASE_REQUIREMENTS = "requirements.txt"
MOBILE_REQUIREMENTS = "requirements-mobile.txt"
ACTIVE_POINTER = BACKEND / ".active-venv"
VENV_ROOT = BACKEND / ".venvs"
BASE_PROBES = ("argon2", "fastapi", "uvicorn")


class DependencyPlanError(RuntimeError):
    pass


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyPlanError(f"invalid {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise DependencyPlanError(f"invalid {path.name}: expected object")
    return value


def _installation_mode() -> str:
    return str(installation_profile.load()["mode"])


def _provider_kinds(state: dict[str, Any] | None = None) -> tuple[str, ...]:
    if state is None:
        profile = installation_profile.load()
        selected = profile.get("provider")
        if selected and installation_profile.selection_pending():
            return (str(selected),)
        config = _read_object(bc_home() / "config.json")
    else:
        selected = None
        config = state
    providers = config.get("providers")
    if not isinstance(providers, list):
        if selected:
            return (str(selected),)
        return ("claude", "codex")
    kinds: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("suspended") is True:
            continue
        kind = str(provider.get("kind") or "").strip()
        spec = provider_manifest.spec_for(kind)
        if spec is None or spec.virtual:
            raise DependencyPlanError(f"unknown active provider kind: {kind or '<empty>'}")
        kinds.add(kind)
    return tuple(sorted(kinds))


def resolve_plan(state: dict[str, Any] | None = None) -> dict:
    mode = _installation_mode()
    kinds = _provider_kinds(state)
    requirements = [BASE_REQUIREMENTS]
    probes = list(BASE_PROBES)
    if mode != "desktop-ui-only":
        requirements.append(MOBILE_REQUIREMENTS)
        probes.append("firebase_admin")
    for kind in kinds:
        spec = provider_manifest.spec_for(kind)
        if spec is None:
            raise DependencyPlanError(f"unknown active provider kind: {kind}")
        requirements.extend(spec.runtime_requirements)
        probes.extend(spec.runtime_probe_imports)
    requirement_names = tuple(dict.fromkeys(requirements))
    probe_names = tuple(dict.fromkeys(probes))
    digest = hashlib.sha256()
    digest.update(json.dumps({
        "mode": mode,
        "requirements": requirement_names,
        "probes": probe_names,
    }, sort_keys=True).encode())
    for name in requirement_names:
        path = BACKEND / name
        if not path.is_file():
            raise DependencyPlanError(f"missing backend requirement group: {name}")
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return {
        "mode": mode,
        "provider_kinds": kinds,
        "requirements": requirement_names,
        "probes": probe_names,
        "hash": digest.hexdigest(),
    }


def _python_in(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _write_pointer(env_dir: Path) -> None:
    relative = env_dir.relative_to(BACKEND)
    ACTIVE_POINTER.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{ACTIVE_POINTER.name}.",
        dir=ACTIVE_POINTER.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(relative.as_posix())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ACTIVE_POINTER)
    finally:
        Path(temporary).unlink(missing_ok=True)


def active_env(backend_dir: Path = BACKEND) -> Path:
    pointer = backend_dir / ACTIVE_POINTER.name
    venv_root = backend_dir / VENV_ROOT.name
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DependencyPlanError("backend dependency environment is not activated") from exc
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise DependencyPlanError("backend dependency environment pointer is invalid")
    env_dir = (backend_dir / relative).resolve()
    runnable = any(
        (env_dir / relative_python).is_file()
        for relative_python in ("bin/python", "Scripts/python.exe")
    )
    if venv_root.resolve() not in env_dir.parents or not runnable:
        raise DependencyPlanError("backend dependency environment pointer is not runnable")
    return env_dir


def assert_active() -> None:
    plan = resolve_plan()
    if active_env().name != plan["hash"]:
        raise DependencyPlanError(
            "provider runtime dependencies changed; restart Better Agent to activate them"
        )


def verified_active_env(backend_dir: Path) -> Path:
    env_dir = active_env(backend_dir)
    python = _python_in(env_dir)
    planner = backend_dir / Path(__file__).name
    if not planner.is_file():
        raise DependencyPlanError("target checkout has no dependency planner")
    try:
        subprocess.run(
            [str(python), str(planner), "assert-active"],
            cwd=backend_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyPlanError(
            "target checkout dependency environment is stale"
        ) from exc
    return env_dir


def _module_available(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


def assert_provider_supported(provider: dict[str, Any]) -> None:
    if provider.get("suspended") is True:
        return
    kind = str(provider.get("kind") or "").strip()
    spec = provider_manifest.spec_for(kind)
    if spec is None or spec.virtual:
        raise DependencyPlanError(f"unknown active provider kind: {kind or '<empty>'}")
    for module in spec.runtime_probe_imports:
        if not _module_available(module):
            raise DependencyPlanError(
                f"{kind} runtime dependencies are not installed; rerun the "
                f"installer with provider={kind} and restart Better Agent"
            )


def assert_state_supported(state: dict[str, Any]) -> None:
    providers = state.get("providers")
    if not isinstance(providers, list):
        raise DependencyPlanError("provider state must contain a providers list")
    for provider in providers:
        if not isinstance(provider, dict):
            raise DependencyPlanError("provider state contains an invalid record")
        assert_provider_supported(provider)


def assert_state_transition_supported(state: dict[str, Any]) -> None:
    current = _read_object(bc_home() / "config.json")
    if not isinstance(current.get("providers"), list):
        return
    current_plan = resolve_plan(current)
    candidate_plan = resolve_plan(state)
    if current_plan["hash"] == candidate_plan["hash"]:
        return
    if active_env().name != candidate_plan["hash"]:
        raise DependencyPlanError(
            "provider runtime dependency plan changed; rerun the installer "
            "before changing active providers"
        )


def _apply_pending_selection(python: Path) -> None:
    if not installation_profile.selection_pending():
        return
    subprocess.run(
        [str(python), str(Path(__file__).resolve()), "apply-selection"],
        cwd=BACKEND,
        check=True,
    )


def _restore_pointer(value: str | None) -> None:
    if value is None:
        ACTIVE_POINTER.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(
        prefix=f".{ACTIVE_POINTER.name}.",
        dir=ACTIVE_POINTER.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ACTIVE_POINTER)
    finally:
        Path(temporary).unlink(missing_ok=True)


def activate(uv: str) -> Path:
    plan = resolve_plan()
    env_dir = VENV_ROOT / str(plan["hash"])
    python = _python_in(env_dir)
    if not python.is_file():
        stage = VENV_ROOT / f".{plan['hash']}.{os.getpid()}.stage"
        shutil.rmtree(stage, ignore_errors=True)
        VENV_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([uv, "venv", str(stage)], check=True)
            command = [uv, "pip", "install", "--python", str(_python_in(stage))]
            for name in plan["requirements"]:
                command.extend(["-r", str(BACKEND / name)])
            subprocess.run(command, cwd=BACKEND, check=True)
            subprocess.run(
                [
                    str(_python_in(stage)),
                    "-c",
                    ";".join(f"import {name}" for name in plan["probes"]),
                ],
                check=True,
            )
            try:
                os.replace(stage, env_dir)
            except OSError:
                if not python.is_file():
                    raise
                shutil.rmtree(stage, ignore_errors=True)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    if installation_profile.selection_pending():
        try:
            previous_pointer = ACTIVE_POINTER.read_text(encoding="utf-8")
        except FileNotFoundError:
            previous_pointer = None
        _write_pointer(env_dir)
        try:
            _apply_pending_selection(python)
        except Exception:
            _restore_pointer(previous_pointer)
            raise
    else:
        _write_pointer(env_dir)
    return env_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("activate", "active", "plan", "apply-selection", "assert-active"),
    )
    parser.add_argument("--uv")
    args = parser.parse_args()
    try:
        if args.command == "activate":
            if not args.uv:
                raise DependencyPlanError("--uv is required for activation")
            value: object = str(activate(args.uv))
        elif args.command == "apply-selection":
            import config_store

            value = config_store.apply_installation_profile_selection()
        elif args.command == "active":
            value = str(active_env())
        elif args.command == "assert-active":
            assert_active()
            value = "active"
        else:
            value = resolve_plan()
        if isinstance(value, dict):
            print(json.dumps(value, sort_keys=True))
        else:
            print(value)
    except (DependencyPlanError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
