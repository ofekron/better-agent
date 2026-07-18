from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json
from .paths import pointer_path, switch_journal_path
from .transaction import mutation_lock


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
        (root / "backend" / ".venv" / sub / executable).is_file()
        for sub, executable in (("bin", "python"), ("Scripts", "python.exe"))
    )


def read() -> dict[str, Any]:
    return read_json(pointer_path())


def resolve(default_dir: str) -> str:
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
    with mutation_lock():
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
    with mutation_lock():
        data = read()
        if request_id and data.get("request_id") != request_id:
            raise ValueError("stale switch request_id")
        data["status"] = status
        data["error"] = error
        data["updated_at"] = time.time()
        _persist(data, "switch_result")
        return data


def revert(reason: str, request_id: str = "") -> dict[str, Any]:
    with mutation_lock():
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
    with mutation_lock():
        data = read()
        if data.get("status") != "switching":
            return False
        if request_id and data.get("request_id") != request_id:
            return False
        revert(reason, request_id)
        return True


def is_switching() -> bool:
    return read().get("status") == "switching"


def confirm_healthy(running_dir: str, request_id: str = "") -> None:
    running = _canonical_checkout(running_dir)
    with mutation_lock():
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
    with mutation_lock():
        data = read()
        if data.get("status") != "switching":
            return False
        from .requests import matches_nonterminal_pointer

        if matches_nonterminal_pointer(data):
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
    commands = parser.add_subparsers(dest="cmd", required=True)
    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("--default", required=True)
    mark_parser = commands.add_parser("mark")
    mark_parser.add_argument("--status", required=True, choices=("active", "failed"))
    mark_parser.add_argument("--error", default="")
    revert_parser = commands.add_parser("revert")
    revert_parser.add_argument("--reason", required=True)
    revert_if_parser = commands.add_parser("revert-if-switching")
    revert_if_parser.add_argument("--reason", required=True)
    revert_if_parser.add_argument("--request-id", default="")
    confirm_parser = commands.add_parser("confirm-healthy")
    confirm_parser.add_argument("--running-dir", required=True)
    confirm_parser.add_argument("--request-id", default="")
    commands.add_parser("request-id")
    commands.add_parser("is-switching")
    commands.add_parser("status")
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
