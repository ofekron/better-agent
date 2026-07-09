"""Active-checkout pointer — which worktree the launchers run the stack from.

Intent is written by the switch-control extension (``set_active``); outcome
is written by the launcher (``mark_result`` / ``revert``) because only the
launcher can respawn the backend and therefore only it can enforce the
auto-revert on a failed start. The schema is deliberately flat so an older
line's code can always read a newer line's pointer.

CLI (used by run.sh):
    python -m daemonhost.pointer resolve --default <dir>
    python -m daemonhost.pointer mark --status active|failed [--error TEXT]
    python -m daemonhost.pointer revert --reason TEXT
    python -m daemonhost.pointer is-switching
    python -m daemonhost.pointer status
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import pointer_path, switch_journal_path


@contextmanager
def _platform_lock(handle, platform: str | None = None):
    platform = platform or os.name
    if platform == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _mutation_lock():
    path = pointer_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        with _platform_lock(handle):
            yield


def _persist(data: dict[str, Any], event: str) -> None:
    write_json(pointer_path(), data)
    journal = switch_journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": event,
        "request_id": str(data.get("request_id") or ""),
        "active": str(data.get("active") or ""),
        "previous": str(data.get("previous") or ""),
        "status": str(data.get("status") or ""),
        "updated_at": data.get("updated_at"),
    }
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _canonical_checkout(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"checkout path must be absolute without traversal: {path}")
    resolved = candidate.resolve()
    if candidate.is_symlink():
        raise ValueError(f"checkout path must not be a symlink: {path}")
    return str(resolved)


def _is_runnable_checkout(path: str) -> bool:
    try:
        root = Path(_canonical_checkout(path))
    except (OSError, ValueError):
        return False
    if not (root / "backend" / "main.py").is_file():
        return False
    return any(
        (root / "backend" / ".venv" / sub / exe).is_file()
        for sub, exe in (("bin", "python"), ("Scripts", "python.exe"))
    )


def read() -> dict[str, Any]:
    return read_json(pointer_path())


def resolve(default_dir: str) -> str:
    """The directory launchers must run from: the pointer's active checkout
    when it is runnable, else the launcher's own directory.

    A ``failed`` switch is never honored — otherwise every future launch keeps
    landing on the target that could not start, with no way back. On failure
    the launcher's own directory (the checkout the operator invoked) is the
    safe home; ``confirm_healthy`` then reconciles the pointer to it."""
    data = read()
    if str(data.get("status") or "").strip() == "failed":
        return default_dir
    active = str(data.get("active") or "").strip()
    if active and _is_runnable_checkout(active):
        return active
    return default_dir


def set_active(path: str, request_id: str) -> dict[str, Any]:
    canonical = _canonical_checkout(path)
    if not request_id.strip():
        raise ValueError("request_id is required")
    if not _is_runnable_checkout(canonical):
        raise ValueError(f"not a runnable checkout: {path}")
    with _mutation_lock():
        current = read()
        if current.get("status") == "switching":
            if current.get("request_id") == request_id and current.get("active") == canonical:
                return current
            raise ValueError("another line switch is already in flight")
        previous = str(current.get("active") or "").strip()
        data = {
            "active": canonical,
            "previous": previous,
            "request_id": request_id,
            "status": "switching",
            "error": "",
            "updated_at": time.time(),
        }
        _persist(data, "switch_requested")
        return data


def mark_result(status: str, error: str = "", request_id: str = "") -> dict[str, Any]:
    if status not in ("active", "failed"):
        raise ValueError(f"invalid pointer status: {status}")
    with _mutation_lock():
        data = read()
        if request_id and data.get("request_id") != request_id:
            raise ValueError("stale switch request_id")
        data["status"] = status
        data["error"] = error
        data["updated_at"] = time.time()
        _persist(data, "switch_result")
        return data


def revert(reason: str, request_id: str = "") -> dict[str, Any]:
    """Swap back to the previous checkout after the active one failed to
    start. No-op (marked failed) when there is no runnable previous."""
    with _mutation_lock():
        data = read()
        if request_id and data.get("request_id") != request_id:
            raise ValueError("stale switch request_id")
        previous = str(data.get("previous") or "").strip()
        if not previous or not _is_runnable_checkout(previous):
            data["status"] = "failed"
            data["error"] = reason
            data["updated_at"] = time.time()
            _persist(data, "switch_failed")
            return data
        data = {
            "active": previous,
            "previous": str(data.get("active") or ""),
            "request_id": str(data.get("request_id") or ""),
            "status": "reverted",
            "error": reason,
            "updated_at": time.time(),
        }
        _persist(data, "switch_reverted")
        return data


def revert_if_switching(reason: str, request_id: str = "") -> bool:
    """Launcher failed-start hook: revert only when a switch is in flight so
    an ordinary crash never flips checkouts and a revert happens once."""
    with _mutation_lock():
        data = read()
        if data.get("status") != "switching":
            return False
        if request_id and data.get("request_id") != request_id:
            return False
        previous = str(data.get("previous") or "").strip()
        if previous and _is_runnable_checkout(previous):
            data["active"], data["previous"] = previous, str(data.get("active") or "")
            data["status"] = "reverted"
        else:
            data["status"] = "failed"
        data["error"] = reason
        data["updated_at"] = time.time()
        _persist(data, "switch_reverted" if data["status"] == "reverted" else "switch_failed")
        return True


def is_switching() -> bool:
    """True when a line switch is in flight (status == ``switching``)."""
    return read().get("status") == "switching"


def confirm_healthy(running_dir: str, request_id: str = "") -> None:
    """Launcher healthy hook, called with the checkout the backend actually
    came up from. Completes an in-flight switch and reconciles a stale pointer
    to reality so ``resolve`` and the UI stop reflecting a dead switch.

    A 'reverted' status pointing at the running checkout is kept so the UI can
    show the revert until the next switch."""
    running = _canonical_checkout(running_dir)
    with _mutation_lock():
        data = read()
        status = str(data.get("status") or "").strip()
        if request_id and data.get("request_id") != request_id:
            raise ValueError("stale switch request_id")
        if status == "switching":
            if str(data.get("active") or "") != running:
                raise ValueError("healthy checkout does not match switch target")
            data["status"] = "active"
            data["error"] = ""
            data["updated_at"] = time.time()
            _persist(data, "switch_confirmed")
            return
        if status == "failed" or str(data.get("active") or "").strip() != running:
            reconciled = {
                "active": running,
                "previous": str(data.get("active") or ""),
                "request_id": str(data.get("request_id") or ""),
                "status": "active",
                "error": "",
                "updated_at": time.time(),
            }
            _persist(reconciled, "pointer_reconciled")


def reconcile_startup() -> bool:
    """Resolve an intent that survived the daemonhost process which observed it."""
    with _mutation_lock():
        data = read()
        if data.get("status") != "switching":
            return False
        previous = str(data.get("previous") or "").strip()
        if previous and _is_runnable_checkout(previous):
            data["active"], data["previous"] = previous, str(data.get("active") or "")
            data["status"] = "reverted"
        else:
            data["status"] = "failed"
        data["error"] = "unfinished switch recovered at daemonhost startup"
        data["updated_at"] = time.time()
        _persist(data, "startup_reconciled")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daemonhost.pointer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--default", required=True)
    p_mark = sub.add_parser("mark")
    p_mark.add_argument("--status", required=True, choices=("active", "failed"))
    p_mark.add_argument("--error", default="")
    p_revert = sub.add_parser("revert")
    p_revert.add_argument("--reason", required=True)
    p_revert_if = sub.add_parser("revert-if-switching")
    p_revert_if.add_argument("--reason", required=True)
    p_revert_if.add_argument("--request-id", default="")
    p_confirm = sub.add_parser("confirm-healthy")
    p_confirm.add_argument("--running-dir", required=True)
    p_confirm.add_argument("--request-id", default="")
    sub.add_parser("request-id")
    sub.add_parser("is-switching")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.cmd == "resolve":
        print(resolve(args.default))
        return 0
    if args.cmd == "mark":
        mark_result(args.status, args.error)
        return 0
    if args.cmd == "revert":
        revert(args.reason)
        return 0
    if args.cmd == "revert-if-switching":
        return 0 if revert_if_switching(args.reason, args.request_id) else 1
    if args.cmd == "confirm-healthy":
        confirm_healthy(args.running_dir, args.request_id)
        return 0
    if args.cmd == "is-switching":
        return 0 if is_switching() else 1
    if args.cmd == "request-id":
        print(str(read().get("request_id") or ""))
        return 0
    print(json.dumps(read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
