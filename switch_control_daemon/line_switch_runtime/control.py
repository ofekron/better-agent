from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json
from .paths import switch_lines_path
from . import pointer

_REQUIRED_CHECKOUT_FILES = (
    "daemonhost/__init__.py",
    "daemonhost/pointer.py",
    "daemonhost/jsonio.py",
    "daemonhost/paths.py",
)
_LINE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _coerce_port(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    if isinstance(value, str) and value.isdigit():
        port = int(value)
        if 1 <= port <= 65535:
            return port
    return None


def _line_target(path: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    target: dict[str, Any] = {"checkout": path}
    home = raw.get("home") or raw.get("ba_home") or raw.get("state_home")
    if home:
        target["home"] = str(home)
    backend_port = _coerce_port(raw.get("backend_port") or raw.get("port"))
    if backend_port is not None:
        target["backend_port"] = backend_port
        target["backend_url"] = f"http://127.0.0.1:{backend_port}"
    elif isinstance(raw.get("backend_url"), str) and raw["backend_url"]:
        target["backend_url"] = raw["backend_url"]
    frontend_port = _coerce_port(raw.get("frontend_port"))
    if frontend_port is not None:
        target["frontend_port"] = frontend_port
    return target


def _conventional_lines(running: str) -> dict[str, dict[str, Any]]:
    if running.endswith("-main"):
        base = running[: -len("-main")]
    elif running.endswith("-qa"):
        base = running[: -len("-qa")]
    else:
        base = running
    candidates = {"dev": base, "qa": f"{base}-qa", "main": f"{base}-main"}
    return {
        name: _line_target(path)
        for name, path in candidates.items()
        if pointer._is_runnable_checkout(path)
    }


def _configured_lines(running_checkout: str) -> dict[str, dict[str, Any]]:
    running = pointer._canonical_checkout(running_checkout)
    raw = read_json(switch_lines_path())
    configured: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _LINE_NAME.fullmatch(name):
            continue
        if isinstance(value, str):
            path_value = value
            config: dict[str, Any] = {}
        elif isinstance(value, dict):
            path_value = str(value.get("checkout") or value.get("path") or value.get("root") or "")
            config = value
        else:
            continue
        if not path_value:
            continue
        try:
            canonical = pointer._canonical_checkout(path_value)
        except (OSError, ValueError):
            continue
        if pointer._is_runnable_checkout(canonical):
            configured[name] = _line_target(canonical, config)
    reconciled = {**_conventional_lines(running), **configured}
    if reconciled != raw:
        write_json(switch_lines_path(), reconciled)
    return reconciled


def _incompatible(path: str) -> list[str]:
    root = Path(path)
    return [relative for relative in _REQUIRED_CHECKOUT_FILES if not (root / relative).is_file()]


def state(running_checkout: str) -> dict[str, Any]:
    running = pointer._canonical_checkout(running_checkout)
    lines = _configured_lines(running)
    line_paths = {name: str(target["checkout"]) for name, target in lines.items()}
    incompatible = {
        name: missing for name, path in line_paths.items() if (missing := _incompatible(path))
    }
    from .requests import read_request

    request_data = read_request()
    request_projection = {
        key: request_data.get(key)
        for key in ("request_id", "target", "status", "error")
        if key in request_data
    }
    return {
        "lines": line_paths,
        "line_targets": lines,
        "running_checkout": running,
        "active_line": next((name for name, path in line_paths.items() if path == running), ""),
        "incompatible": incompatible,
        "pointer": pointer.read(),
        "request": request_projection,
        "switchable": len(lines) >= 2,
    }


def request(running_checkout: str, target: str, request_id: str) -> dict[str, Any]:
    snapshot = state(running_checkout)
    lines = snapshot["lines"]
    if target not in lines:
        raise ValueError(f"unknown line: {target!r}")
    target_path = str(lines[target])
    current_pointer = pointer.read()
    if (
        current_pointer.get("status") == "switching"
        and current_pointer.get("request_id") == request_id
        and current_pointer.get("active") == target_path
    ):
        return {"request_id": request_id, "target": target}
    if target_path == snapshot["running_checkout"]:
        raise ValueError(f"line {target!r} is already active")
    missing = _incompatible(target_path)
    if missing:
        raise ValueError(f"line {target!r} cannot run switch control (missing {', '.join(missing)})")
    line_target = snapshot.get("line_targets", {}).get(target, {})
    target_url = line_target.get("backend_url") if isinstance(line_target, dict) else ""
    if isinstance(target_url, str) and target_url:
        return {"request_id": request_id, "target": target, "status": "succeeded", "target_url": target_url}
    pointer.set_active(target_path, request_id)
    return {"request_id": request_id, "target": target}
