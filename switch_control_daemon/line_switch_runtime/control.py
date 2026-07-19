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


def _conventional_lines(running: str) -> dict[str, str]:
    if running.endswith("-main"):
        base = running[: -len("-main")]
    elif running.endswith("-qa"):
        base = running[: -len("-qa")]
    else:
        base = running
    candidates = {"dev": base, "qa": f"{base}-qa", "main": f"{base}-main"}
    return {
        name: path
        for name, path in candidates.items()
        if pointer._is_runnable_checkout(path)
    }


def _configured_lines(running_checkout: str) -> dict[str, str]:
    running = pointer._canonical_checkout(running_checkout)
    raw = read_json(switch_lines_path())
    configured: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _LINE_NAME.fullmatch(name) or not isinstance(value, str):
            continue
        try:
            canonical = pointer._canonical_checkout(value)
        except (OSError, ValueError):
            continue
        if pointer._is_runnable_checkout(canonical):
            configured[name] = canonical
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
    incompatible = {
        name: missing for name, path in lines.items() if (missing := _incompatible(path))
    }
    from .requests import read_request

    request_data = read_request()
    request_projection = {
        key: request_data.get(key)
        for key in ("request_id", "target", "status", "error")
        if key in request_data
    }
    return {
        "lines": lines,
        "running_checkout": running,
        "active_line": next((name for name, path in lines.items() if path == running), ""),
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
    target_path = lines[target]
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
    pointer.set_active(target_path, request_id)
    return {"request_id": request_id, "target": target}
