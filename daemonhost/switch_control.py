from __future__ import annotations

from pathlib import Path
from typing import Any

from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import ba_home
from daemonhost import pointer

_REQUIRED_CHECKOUT_FILES = (
    "daemonhost/__init__.py",
    "daemonhost/pointer.py",
    "daemonhost/jsonio.py",
    "daemonhost/paths.py",
)


def _configured_lines(running_checkout: str) -> dict[str, str]:
    running = pointer._canonical_checkout(running_checkout)
    config_path = ba_home() / "switch_lines.json"
    raw = read_json(config_path)
    lines: dict[str, str] = {}
    for name in ("dev", "main"):
        value = raw.get(name)
        if not isinstance(value, str):
            continue
        try:
            canonical = pointer._canonical_checkout(value)
        except (OSError, ValueError):
            continue
        if pointer._is_runnable_checkout(canonical):
            lines[name] = canonical
    if lines:
        return lines
    seed = (
        {"main": running, "dev": running[: -len("-main")]}
        if running.endswith("-main")
        else {"dev": running, "main": running + "-main"}
    )
    lines = {name: path for name, path in seed.items() if pointer._is_runnable_checkout(path)}
    if lines:
        write_json(config_path, lines)
    return lines


def _incompatible(path: str) -> list[str]:
    root = Path(path)
    return [relative for relative in _REQUIRED_CHECKOUT_FILES if not (root / relative).is_file()]


def state(running_checkout: str) -> dict[str, Any]:
    running = pointer._canonical_checkout(running_checkout)
    lines = _configured_lines(running)
    incompatible = {
        name: missing for name, path in lines.items() if (missing := _incompatible(path))
    }
    return {
        "lines": lines,
        "running_checkout": running,
        "active_line": next((name for name, path in lines.items() if path == running), ""),
        "incompatible": incompatible,
        "pointer": pointer.read(),
        "switchable": len(lines) >= 2,
    }


def request(running_checkout: str, target: str, request_id: str) -> dict[str, Any]:
    snapshot = state(running_checkout)
    lines = snapshot["lines"]
    if target not in lines:
        raise ValueError(f"unknown line: {target!r}")
    target_path = lines[target]
    if target_path == snapshot["running_checkout"]:
        raise ValueError(f"line {target!r} is already active")
    missing = _incompatible(target_path)
    if missing:
        raise ValueError(
            f"line {target!r} cannot run switch control (missing {', '.join(missing)})"
        )
    pointer.set_active(target_path, request_id)
    return {"request_id": request_id, "target": target}


def abort(request_id: str, reason: str) -> dict[str, Any]:
    return pointer.revert(reason, request_id)
