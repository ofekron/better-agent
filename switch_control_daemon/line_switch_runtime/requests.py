from __future__ import annotations

import os
import time
import uuid
from typing import Any

from . import pointer
from .control import request as set_switch_intent, state
from .jsonio import read_json, write_json, write_text
from .paths import refresh_result_path, restart_request_path, switch_request_path
from .transaction import mutation_lock

_NONTERMINAL = {"pending", "accepted"}
_LEGACY_RECONCILE_ERROR = "unfinished switch recovered at daemonhost startup"


def read_request() -> dict[str, Any]:
    return read_json(switch_request_path())


def _persist(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = time.time()
    write_json(switch_request_path(), data)
    return data


def submit(
    running_checkout: str,
    target: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with mutation_lock():
        snapshot = state(running_checkout)
        if target not in snapshot["lines"]:
            raise ValueError(f"unknown line: {target!r}")
        if snapshot["incompatible"].get(target):
            raise ValueError(f"line {target!r} is incompatible")
        existing = read_request()
        if existing.get("status") in _NONTERMINAL:
            if existing.get("target") == target:
                return existing
            raise ValueError("another line switch request is already in flight")
        request_id = request_id or str(uuid.uuid4())
        return _persist(
            {
                "version": 1,
                "request_id": request_id,
                "target": target,
                "target_path": snapshot["lines"][target],
                "running_checkout": snapshot["running_checkout"],
                "status": "pending",
                "error": "",
                "created_at": time.time(),
            }
        )


def matches_nonterminal_pointer(pointer_data: dict[str, Any]) -> bool:
    request = read_request()
    return (
        request.get("status") in _NONTERMINAL
        and request.get("request_id") == pointer_data.get("request_id")
        and request.get("target_path") == pointer_data.get("active")
    )


def _restart(request_id: str) -> None:
    write_text(restart_request_path(), request_id)


def _mark_failed(data: dict[str, Any], error: str, restart: bool = False) -> dict[str, Any]:
    pointer_data = pointer.read()
    if pointer_data.get("request_id") == data.get("request_id") and pointer_data.get("status") == "switching":
        pointer.revert(error, str(data["request_id"]))
    data["status"] = "failed"
    data["error"] = error
    _persist(data)
    if restart:
        _restart(str(data["request_id"]))
    return data


def service_tick(running_checkout: str | None = None) -> dict[str, Any]:
    with mutation_lock():
        data = read_request()
        if not data:
            return {}
        status = str(data.get("status") or "")
        if status not in _NONTERMINAL:
            return data
        request_id = str(data.get("request_id") or "")
        target_path = str(data.get("target_path") or "")
        running = running_checkout or str(data.get("running_checkout") or "")
        pointer_data = pointer.read()

        if status == "pending":
            if pointer_data.get("request_id") == request_id and pointer_data.get("active") == target_path:
                data["status"] = "accepted"
                _persist(data)
            else:
                set_switch_intent(running, str(data.get("target") or ""), request_id)
                data["status"] = "accepted"
                _persist(data)
            _restart(request_id)
            return data

        if pointer_data.get("request_id") != request_id:
            if pointer_data.get("active") == running and pointer_data.get("status") in {"active", "reverted", "failed"}:
                data["status"] = "pending"
                _persist(data)
                return service_tick(running)
            return _mark_failed(data, "switch pointer ownership was lost")

        pointer_status = str(pointer_data.get("status") or "")
        if pointer_status in {"reverted", "failed"}:
            return _mark_failed(data, str(pointer_data.get("error") or "line switch failed"))
        if pointer_status == "switching":
            _restart(request_id)
            return data
        if pointer_status != "active" or pointer_data.get("active") != target_path:
            return data

        refresh = read_json(refresh_result_path())
        if refresh.get("request_id") != request_id:
            return data
        refresh_status = str(refresh.get("status") or "")
        if refresh_status == "succeeded":
            data["status"] = "succeeded"
            data["error"] = ""
            return _persist(data)
        if refresh_status == "failed":
            return _mark_failed(data, str(refresh.get("error") or "frontend build failed"), restart=True)
        return data


def request_status(request_id: str) -> dict[str, Any]:
    data = read_request()
    if data.get("request_id") != request_id:
        return {"request_id": request_id, "status": "pending", "error": "", "found": False}
    return {
        "request_id": request_id,
        "status": data.get("status", "pending"),
        "error": str(data.get("error") or ""),
        "found": True,
    }


def _remove_own_restart(request_id: str) -> None:
    path = restart_request_path()
    try:
        if path.read_text(encoding="utf-8") == request_id:
            path.unlink()
    except OSError:
        return


def cleanup_bootstrap(request_id: str, reason: str) -> None:
    with mutation_lock():
        data = read_request()
        if data.get("request_id") != request_id:
            return
        _mark_failed(data, reason)
        _remove_own_restart(request_id)


def bootstrap(
    running_checkout: str,
    target: str,
    *,
    timeout: float = 180.0,
    poll_interval: float = 0.25,
    request_id: str | None = None,
) -> dict[str, Any]:
    data = submit(running_checkout, target, request_id)
    request_id = str(data["request_id"])
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            with mutation_lock():
                pointer_data = pointer.read()
                data = read_request()
                if data.get("status") == "succeeded":
                    return data
                if data.get("status") == "failed":
                    error = str(data.get("error") or "")
                    if error != _LEGACY_RECONCILE_ERROR:
                        raise RuntimeError(error or "line switch failed")
                    data["status"] = "pending"
                    data["error"] = ""
                    _persist(data)
                elif (
                    pointer_data.get("request_id") == request_id
                    and pointer_data.get("status") in {"reverted", "failed"}
                ):
                    error = str(pointer_data.get("error") or "")
                    if error != _LEGACY_RECONCILE_ERROR:
                        _mark_failed(data, error or "line switch failed")
                        raise RuntimeError(error or "line switch failed")
                    data["status"] = "pending"
                    data["error"] = ""
                    _persist(data)
                service_tick(running_checkout)
            time.sleep(poll_interval)
    except (KeyboardInterrupt, SystemExit):
        cleanup_bootstrap(request_id, "bootstrap interrupted")
        raise
    except RuntimeError:
        raise
    except BaseException:
        cleanup_bootstrap(request_id, "bootstrap failed")
        raise
    cleanup_bootstrap(request_id, "bootstrap timed out")
    raise TimeoutError("line switch bootstrap timed out")
