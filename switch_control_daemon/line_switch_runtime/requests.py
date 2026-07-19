from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from . import pointer
from .control import request as set_switch_intent, state
from .jsonio import read_json, write_json, write_text
from .paths import refresh_result_path, restart_request_path, switch_request_path
from .transaction import mutation_lock

_NONTERMINAL = {"preparing", "pending", "accepted"}
_LEGACY_RECONCILE_ERROR = "unfinished switch recovered at daemonhost startup"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_LINE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PREPARATION_HANDLES: dict[str, Any] = {}


def _owner_lock_path():
    return switch_request_path().with_suffix(".owner.lock")


def _lock_handle(handle, *, blocking: bool) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        return False
    return True


def _unlock_handle(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_preparation_owner(token: str) -> bool:
    path = _owner_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    if not _lock_handle(handle, blocking=False):
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(token.encode("ascii"))
    handle.flush()
    os.fsync(handle.fileno())
    _PREPARATION_HANDLES[token] = handle
    return True


def _release_preparation_owner(token: str) -> None:
    handle = _PREPARATION_HANDLES.pop(token, None)
    if handle is None:
        return
    try:
        _unlock_handle(handle)
    finally:
        handle.close()


def _preparation_owner_alive(token: object) -> bool:
    if not isinstance(token, str) or not token:
        return False
    if token in _PREPARATION_HANDLES:
        return True
    path = _owner_lock_path()
    try:
        handle = path.open("r+b")
    except OSError:
        return False
    try:
        if _lock_handle(handle, blocking=False):
            _unlock_handle(handle)
            return False
        try:
            return path.read_text(encoding="ascii") == token
        except OSError:
            return False
    finally:
        handle.close()


def read_request() -> dict[str, Any]:
    return read_json(switch_request_path())


def _persist(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = time.time()
    write_json(switch_request_path(), data)
    return data


def _reservation_result(data: dict[str, Any], created: bool, token: str = "") -> dict[str, Any]:
    result = dict(data)
    result.pop("preparation_owner_pid", None)
    result.pop("preparation_token", None)
    result["_reservation_created"] = created
    if created:
        result["_preparation_token"] = token
    return result


def reserve(
    running_checkout: str,
    target: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with mutation_lock():
        running_checkout = pointer._canonical_checkout(running_checkout)
        pointer_data = pointer.read()
        active = str(pointer_data.get("active") or "")
        if active and pointer._is_runnable_checkout(active) and active != running_checkout:
            raise ValueError("running checkout does not match the active launcher pointer")
        if not _LINE_NAME.fullmatch(target):
            raise ValueError("invalid line name")
        snapshot = state(running_checkout)
        if target not in snapshot["lines"]:
            raise ValueError(f"unknown line: {target!r}")
        if snapshot["incompatible"].get(target):
            raise ValueError(f"line {target!r} is incompatible")
        existing = read_request()
        if existing.get("status") in _NONTERMINAL:
            if existing.get("target") == target:
                if existing.get("status") == "preparing" and not _preparation_owner_alive(existing.get("preparation_token")):
                    token = uuid.uuid4().hex
                    if not _acquire_preparation_owner(token):
                        raise ValueError("line switch preparation owner is unavailable")
                    existing["preparation_token"] = token
                    try:
                        _persist(existing)
                    except BaseException:
                        _release_preparation_owner(token)
                        raise
                    return _reservation_result(existing, True, token)
                return _reservation_result(existing, False)
            raise ValueError("another line switch request is already in flight")
        request_id = request_id or str(uuid.uuid4())
        if not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("invalid request id")
        refresh = read_json(refresh_result_path())
        if refresh.get("request_id") == request_id:
            raise ValueError("request id already has restart result evidence")
        token = uuid.uuid4().hex
        if not _acquire_preparation_owner(token):
            raise ValueError("line switch preparation owner is unavailable")
        try:
            persisted = _persist(
                {
                    "version": 1,
                    "request_id": request_id,
                    "target": target,
                    "target_path": snapshot["lines"][target],
                    "running_checkout": snapshot["running_checkout"],
                    "status": "preparing",
                    "preparation_token": token,
                    "error": "",
                    "created_at": time.time(),
                }
            )
        except BaseException:
            _release_preparation_owner(token)
            raise
        return _reservation_result(persisted, True, token)


def activate(request_id: str, preparation_token: str) -> dict[str, Any]:
    with mutation_lock():
        data = read_request()
        if data.get("request_id") != request_id:
            raise ValueError("line switch reservation was lost")
        if data.get("status") == "preparing":
            if data.get("preparation_token") != preparation_token:
                raise ValueError("line switch preparation ownership was lost")
            data["status"] = "pending"
            data.pop("preparation_token", None)
            result = _persist(data)
            _release_preparation_owner(preparation_token)
            return result
        return data


def fail(request_id: str, error: str, preparation_token: str) -> dict[str, Any]:
    with mutation_lock():
        data = read_request()
        if data.get("request_id") != request_id:
            raise ValueError("line switch reservation was lost")
        if data.get("status") == "preparing" and data.get("preparation_token") != preparation_token:
            raise ValueError("line switch preparation ownership was lost")
        if data.get("status") not in _NONTERMINAL:
            return data
        result = _mark_failed(data, error)
        _release_preparation_owner(preparation_token)
        return result


def submit(
    running_checkout: str,
    target: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    data = reserve(running_checkout, target, request_id)
    created = bool(data.pop("_reservation_created", False))
    preparation_token = str(data.pop("_preparation_token", ""))
    if data.get("status") == "preparing" and created:
        return activate(str(data["request_id"]), preparation_token)
    return data


def matches_nonterminal_pointer(pointer_data: dict[str, Any]) -> bool:
    request = read_request()
    return (
        request.get("status") in _NONTERMINAL
        and request.get("request_id") == pointer_data.get("request_id")
        and request.get("target_path") == pointer_data.get("active")
    )


def _validate_record(
    data: dict[str, Any],
    pointer_data: dict[str, Any],
    running_checkout: str | None,
) -> str:
    request_id = data.get("request_id")
    target = data.get("target")
    running = data.get("running_checkout")
    target_path = data.get("target_path")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return "invalid durable request id"
    if not isinstance(target, str) or not _LINE_NAME.fullmatch(target):
        return "invalid durable target line"
    if not isinstance(running, str) or not isinstance(target_path, str):
        return "invalid durable checkout fields"
    try:
        running = pointer._canonical_checkout(running)
        target_path = pointer._canonical_checkout(target_path)
    except (OSError, ValueError):
        return "invalid durable checkout path"
    owns_pointer = pointer_data.get("request_id") == request_id
    if owns_pointer:
        pointer_status = pointer_data.get("status")
        forward_owned = (
            pointer_status in {"switching", "active", "failed"}
            and pointer_data.get("active") == target_path
            and pointer_data.get("previous") in {"", running}
        )
        reverted_owned = (
            pointer_status == "reverted"
            and pointer_data.get("active") == running
            and pointer_data.get("previous") == target_path
        )
        if not forward_owned and not reverted_owned:
            return "durable request does not match pointer ownership"
    elif pointer_data.get("active") != running:
        return "durable running checkout does not match active pointer"
    if running_checkout:
        try:
            live_checkout = pointer._canonical_checkout(running_checkout)
        except (OSError, ValueError):
            return "invalid supervisor checkout"
        expected_live = (
            target_path
            if owns_pointer and pointer_data.get("status") == "active"
            else running
        )
        if live_checkout != expected_live:
            return "supervisor checkout does not match durable request"
    try:
        snapshot = state(running)
    except (OSError, ValueError):
        return "invalid durable running checkout"
    if snapshot["lines"].get(target) != target_path:
        return "durable target does not match discovered line"
    return ""


def _restart(request_id: str) -> None:
    write_text(restart_request_path(), request_id)


def _mark_failed(data: dict[str, Any], error: str, restart: bool = False) -> dict[str, Any]:
    preparation_token = str(data.get("preparation_token") or "")
    pointer_data = pointer.read()
    owns_target = (
        pointer_data.get("request_id") == data.get("request_id")
        and pointer_data.get("status") in {"switching", "active"}
    )
    if owns_target:
        pointer.revert(error, str(data["request_id"]))
        restart = True
    if restart:
        _restart(str(data["request_id"]))
    data["status"] = "failed"
    data["error"] = error
    data.pop("preparation_owner_pid", None)
    data.pop("preparation_token", None)
    data["reconciled_at"] = time.time()
    result = _persist(data)
    _release_preparation_owner(preparation_token)
    return result


def service_tick(running_checkout: str | None = None) -> dict[str, Any]:
    with mutation_lock():
        data = read_request()
        if not data:
            return {}
        status = str(data.get("status") or "")
        if status not in _NONTERMINAL:
            if status == "failed" and not data.get("reconciled_at"):
                return _mark_failed(data, str(data.get("error") or "line switch failed"))
            return data
        pointer_data = pointer.read()
        validation_error = _validate_record(data, pointer_data, running_checkout)
        if validation_error:
            return _mark_failed(data, validation_error)
        if status == "preparing":
            if not _preparation_owner_alive(data.get("preparation_token")):
                return _mark_failed(data, "line switch preparation was abandoned")
            return data
        request_id = str(data.get("request_id") or "")
        target_path = str(data.get("target_path") or "")
        running = running_checkout or str(data.get("running_checkout") or "")

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
                service_tick()
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
