#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent

import installation_capabilities
import installation_profile
import provider_manifest
from paths import bc_home

BASE_REQUIREMENTS = "requirements.txt"
MOBILE_REQUIREMENTS = "requirements-mobile.txt"
ACTIVE_POINTER = BACKEND / ".active-venv"
VENV_ROOT = BACKEND / ".venvs"
PLAN_MARKER = ".dependency-plan.json"
BASE_PROBES = ("argon2", "fastapi", "uvicorn", "watchfiles", "mcp.server.fastmcp")


class DependencyPlanError(RuntimeError):
    pass


@contextmanager
def _windows_activation_mutex(path: Path):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    name = "Local\\BetterAgentDependencyPlan-" + hashlib.sha256(
        str(path.resolve()).casefold().encode()
    ).hexdigest()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
    if wait_result not in (0, 0x80):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "WaitForSingleObject failed")
    try:
        yield
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextmanager
def activation_lock():
    VENV_ROOT.mkdir(parents=True, exist_ok=True)
    path = VENV_ROOT.parent / ".dependency-plan.lock"
    if os.name == "nt":
        with _windows_activation_mutex(path):
            yield
        return
    with open(path, "a+b") as handle:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _installation_mode(profile: dict[str, Any] | None = None) -> str:
    selected = installation_profile.load() if profile is None else profile
    mode = selected.get("mode")
    return str(mode) if mode in installation_profile.MODES else "setup-required"


def _mobile_requested(profile: dict[str, Any] | None = None) -> bool:
    """Whether the dependency set must carry mobile support.

    Reads the capability the user currently wants, not the install-time mode:
    enabling mobile later must change the plan so the next activation installs
    what it needs.
    """
    selected = installation_profile.load() if profile is None else profile
    if selected.get("status") != "active":
        return False
    return installation_capabilities.settings(selected.get("mode"))[
        installation_capabilities.MOBILE
    ]


def _node_runtime() -> bool:
    return bool(
        os.environ.get("BETTER_AGENT_NODE_ID")
        or os.environ.get("BETTER_CLAUDE_NODE_ID")
    )


def _provider_kinds(
    state: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    if state is None:
        selected_profile = installation_profile.load() if profile is None else profile
        selected = selected_profile.get("provider")
        if selected and (
            profile is not None or installation_profile.selection_pending()
        ):
            return (str(selected),)
        if selected_profile.get("status") != "active" and not _node_runtime():
            return ()
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
        if not isinstance(provider, dict):
            continue
        kind = str(provider.get("kind") or "").strip()
        spec = provider_manifest.spec_for(kind)
        if spec is None or spec.virtual:
            raise DependencyPlanError(f"unknown provider kind: {kind or '<empty>'}")
        kinds.add(kind)
    return tuple(sorted(kinds))


def resolve_plan(
    state: dict[str, Any] | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> dict:
    mode = _installation_mode(profile)
    kinds = _provider_kinds(state, profile)
    requirements = [BASE_REQUIREMENTS]
    probes = list(BASE_PROBES)
    mobile = _mobile_requested(profile)
    if mobile:
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
        "mobile": mobile,
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


def active_env(backend_dir: Path | None = None) -> Path:
    backend_dir = BACKEND if backend_dir is None else backend_dir
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
    _assert_environment(active_env(), plan)


def assert_active_plan() -> None:
    plan = resolve_plan()
    marker = _read_object(active_env() / PLAN_MARKER)
    if marker != {"schema_version": 1, "hash": plan["hash"]}:
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
            [str(python), str(planner), "assert-active-plan"],
            cwd=backend_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyPlanError(
            "target checkout dependency environment is stale"
        ) from exc
    return env_dir


def verified_active_python(backend_dir: Path) -> Path:
    return _python_in(verified_active_env(backend_dir))


@functools.lru_cache(maxsize=None)
def _module_available(module: str) -> bool:
    import importlib.util

    # A probe whose parent package is missing raises rather than reporting
    # absence, and this answer now gates provider resolution — an unavailable
    # module must read as unavailable, never as a crash.
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _probe_runtime_module(python: Path, module: str) -> None:
    subprocess.run(
        [str(python), "-c", f"import {module}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _runtime_probe_workers(probe_count: int, platform: str = os.name) -> int:
    return 1 if platform == "nt" else min(probe_count, 8)


def _probe_environment(env_dir: Path, probes: tuple[str, ...]) -> None:
    if not probes:
        return
    python = _python_in(env_dir)
    failures: list[tuple[str, BaseException]] = []
    workers = _runtime_probe_workers(len(probes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {
            executor.submit(_probe_runtime_module, python, module): module
            for module in probes
        }
        for future in concurrent.futures.as_completed(pending):
            try:
                future.result()
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append((pending[future], exc))
    if not failures:
        return
    modules = ", ".join(sorted(module for module, _ in failures))
    raise DependencyPlanError(
        f"backend dependency environment failed runtime module probes: {modules}"
    ) from failures[0][1]


def _assert_environment(env_dir: Path, plan: dict[str, Any]) -> None:
    marker = _read_object(env_dir / PLAN_MARKER)
    if marker != {"schema_version": 1, "hash": plan["hash"]}:
        raise DependencyPlanError(
            "provider runtime dependencies changed; restart Better Agent to activate them"
        )
    _probe_environment(env_dir, tuple(plan["probes"]))


def assert_provider_supported(provider: dict[str, Any]) -> None:
    """Validate the provider kind. Configuring a provider never depends on its
    runtime dependencies already being installed — the next activation installs
    what the configured set needs. Whether a provider can run *right now* is
    `provider_runtime_pending`."""
    kind = str(provider.get("kind") or "").strip()
    spec = provider_manifest.spec_for(kind)
    if spec is None or spec.virtual:
        raise DependencyPlanError(f"unknown provider kind: {kind or '<empty>'}")


def assert_state_supported(state: dict[str, Any]) -> None:
    providers = state.get("providers")
    if not isinstance(providers, list):
        raise DependencyPlanError("provider state must contain a providers list")
    for provider in providers:
        if not isinstance(provider, dict):
            raise DependencyPlanError("provider state contains an invalid record")
        assert_provider_supported(provider)


def provider_runtime_pending(kind: str) -> bool:
    """Whether the provider is configured but its runtime is not importable yet,
    i.e. it needs the restart that installs the newly planned dependencies."""
    spec = provider_manifest.spec_for(kind)
    if spec is None:
        return False
    return any(
        not _module_available(module) for module in spec.runtime_probe_imports
    )


def assert_provider_runtime_ready(kind: str) -> None:
    if not provider_runtime_pending(kind):
        return
    # Only a runtime that can install what it plans is one restart away.
    if installation_capabilities.self_provisionable():
        raise DependencyPlanError(
            f"{kind} runtime dependencies are being installed; restart Better "
            f"Agent to finish activating them"
        )
    raise DependencyPlanError(
        f"{kind} is not part of this build; install Better Agent from source "
        f"to use it"
    )


def _commit_activation(python: Path, make_default: bool = False) -> None:
    if not installation_profile.selection_pending():
        return
    if installation_profile.refresh_activation_receipt():
        return
    subprocess.run(
        [
            str(python),
            str(Path(__file__).resolve()),
            "apply-selection",
            *(("--make-default",) if make_default else ()),
        ],
        cwd=BACKEND,
        check=True,
        stdout=subprocess.DEVNULL,
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


def _build_environment(uv: str, plan: dict[str, Any]) -> Path:
    plan_root = VENV_ROOT / str(plan["hash"])
    build_id = uuid.uuid4().hex
    stage = plan_root / f".stage-{build_id}"
    env_dir = plan_root / build_id
    plan_root.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [uv, "venv", "--relocatable", str(stage)],
            check=True,
            stdout=sys.stderr,
        )
        command = [uv, "pip", "install", "--python", str(_python_in(stage))]
        for name in plan["requirements"]:
            command.extend(["-r", str(BACKEND / name)])
        subprocess.run(command, cwd=BACKEND, check=True, stdout=sys.stderr)
        _probe_environment(stage, tuple(plan["probes"]))
        (stage / PLAN_MARKER).write_text(
            json.dumps({"schema_version": 1, "hash": plan["hash"]}),
            encoding="utf-8",
        )
        os.replace(stage, env_dir)
        _assert_environment(env_dir, plan)
        return env_dir
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _resolve_or_build_environment(uv: str, plan: dict[str, Any]) -> Path:
    try:
        env_dir = active_env()
        _assert_environment(env_dir, plan)
        return env_dir
    except DependencyPlanError:
        return _build_environment(uv, plan)


def prepare_installation(uv: str, profile: dict[str, Any]) -> Path:
    plan = resolve_plan(profile=profile)
    env_dir = _resolve_or_build_environment(uv, plan)
    latest_plan = resolve_plan(profile=profile)
    if latest_plan["hash"] != plan["hash"]:
        raise DependencyPlanError(
            "dependency plan changed during installation; rerun the installer"
        )
    _assert_environment(env_dir, plan)
    return env_dir


def activate_prepared_installation(
    env_dir: Path,
    profile: dict[str, Any],
    *,
    make_default: bool = True,
) -> Path:
    """Activate a prepared installation.

    `make_default` carries whether the provider was *chosen*. Setup asks the
    user, so their answer moves the default. Adoption only infers a provider
    from what happens to be on the machine, so it must not seize a default the
    user already had.
    """
    plan = resolve_plan(profile=profile)
    _assert_environment(env_dir, plan)
    try:
        previous_pointer = ACTIVE_POINTER.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous_pointer = None
    installation_profile.stage_activation(profile)
    _write_pointer(env_dir)
    try:
        _commit_activation(_python_in(env_dir), make_default=make_default)
        if installation_profile.selection_pending():
            raise DependencyPlanError(
                "installation activation receipt was not committed"
            )
    except BaseException:
        _restore_pointer(previous_pointer)
        raise
    return env_dir


def activate(uv: str) -> Path:
    with activation_lock():
        plan = resolve_plan()
        env_dir = _resolve_or_build_environment(uv, plan)
        latest_plan = resolve_plan()
        if latest_plan["hash"] != plan["hash"]:
            raise DependencyPlanError(
                "dependency plan changed during activation; rerun the installer"
            )
        try:
            previous_pointer = ACTIVE_POINTER.read_text(encoding="utf-8")
        except FileNotFoundError:
            previous_pointer = None
        _write_pointer(env_dir)
        try:
            _commit_activation(_python_in(env_dir))
        except Exception:
            _restore_pointer(previous_pointer)
            raise
        return env_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "activate",
            "active",
            "plan",
            "apply-selection",
            "assert-active",
            "assert-active-plan",
        ),
    )
    parser.add_argument("--uv")
    parser.add_argument("--make-default", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "activate":
            if not args.uv:
                raise DependencyPlanError("--uv is required for activation")
            value: object = str(activate(args.uv))
        elif args.command == "apply-selection":
            import config_store

            value = config_store.apply_installation_profile_selection(
                make_default=args.make_default,
            )
        elif args.command == "active":
            value = str(active_env())
        elif args.command == "assert-active":
            assert_active()
            value = "active"
        elif args.command == "assert-active-plan":
            assert_active_plan()
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
