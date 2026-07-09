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
import json
import sys
import time
from pathlib import Path
from typing import Any

from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import pointer_path


def _is_runnable_checkout(path: str) -> bool:
    root = Path(path)
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
    if not _is_runnable_checkout(path):
        raise ValueError(f"not a runnable checkout: {path}")
    current = read()
    previous = str(current.get("active") or "").strip()
    data = {
        "active": str(Path(path).resolve()),
        "previous": previous,
        "request_id": request_id,
        "status": "switching",
        "error": "",
        "updated_at": time.time(),
    }
    write_json(pointer_path(), data)
    return data


def mark_result(status: str, error: str = "") -> dict[str, Any]:
    if status not in ("active", "failed"):
        raise ValueError(f"invalid pointer status: {status}")
    data = read()
    data["status"] = status
    data["error"] = error
    data["updated_at"] = time.time()
    write_json(pointer_path(), data)
    return data


def revert(reason: str) -> dict[str, Any]:
    """Swap back to the previous checkout after the active one failed to
    start. No-op (marked failed) when there is no runnable previous."""
    data = read()
    previous = str(data.get("previous") or "").strip()
    if not previous or not _is_runnable_checkout(previous):
        return mark_result("failed", reason)
    data = {
        "active": previous,
        "previous": str(data.get("active") or ""),
        "request_id": str(data.get("request_id") or ""),
        "status": "reverted",
        "error": reason,
        "updated_at": time.time(),
    }
    write_json(pointer_path(), data)
    return data


def revert_if_switching(reason: str) -> bool:
    """Launcher failed-start hook: revert only when a switch is in flight so
    an ordinary crash never flips checkouts and a revert happens once."""
    if read().get("status") != "switching":
        return False
    revert(reason)
    return True


def is_switching() -> bool:
    """True when a line switch is in flight (status == ``switching``)."""
    return read().get("status") == "switching"


def confirm_healthy(running_dir: str) -> None:
    """Launcher healthy hook, called with the checkout the backend actually
    came up from. Completes an in-flight switch and reconciles a stale pointer
    to reality so ``resolve`` and the UI stop reflecting a dead switch.

    A 'reverted' status pointing at the running checkout is kept so the UI can
    show the revert until the next switch."""
    data = read()
    status = str(data.get("status") or "").strip()
    running = str(Path(running_dir).resolve())
    if status == "switching":
        mark_result("active")
        return
    if status == "failed" or str(data.get("active") or "").strip() != running:
        write_json(
            pointer_path(),
            {
                "active": running,
                "previous": str(data.get("active") or ""),
                "request_id": str(data.get("request_id") or ""),
                "status": "active",
                "error": "",
                "updated_at": time.time(),
            },
        )


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
    p_confirm = sub.add_parser("confirm-healthy")
    p_confirm.add_argument("--running-dir", required=True)
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
        return 0 if revert_if_switching(args.reason) else 1
    if args.cmd == "confirm-healthy":
        confirm_healthy(args.running_dir)
        return 0
    if args.cmd == "is-switching":
        return 0 if is_switching() else 1
    print(json.dumps(read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
