"""Switch journal daemon (supervisor lifecycle).

Outlives backend restarts, so it is the one component that can truthfully
record a switch timeline across the restart gap and catch a stuck switch
(launcher died mid-swap). It never restarts anything itself — the launcher
owns respawn and auto-revert; this daemon observes and journals.

Journal: ba_home()/switch_journal.jsonl, append-only
{ts, event, request_id, active, previous, status}.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

POLL_SECONDS = 2.0
# A pointer stuck in "switching" longer than this means the launcher died
# mid-swap; mark it failed so the UI stops showing progress.
STUCK_SWITCH_SECONDS = 300.0


def _ba_home() -> Path:
    for var in ("BETTER_AGENT_HOME", "BETTER_CLAUDE_HOME"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".better-claude"


def _backend_port() -> str:
    for var in ("BETTER_AGENT_BACKEND_PORT", "BETTER_CLAUDE_BACKEND_PORT"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "18765"


def _backend_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_backend_port()}/healthz", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def _read_pointer() -> dict:
    try:
        data = json.loads((_ba_home() / "active_checkout.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_pointer(data: dict) -> None:
    path = _ba_home() / "active_checkout.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _journal(event: str, pointer: dict) -> None:
    entry = {
        "ts": time.time(),
        "event": event,
        "request_id": str(pointer.get("request_id") or ""),
        "active": str(pointer.get("active") or ""),
        "previous": str(pointer.get("previous") or ""),
        "status": str(pointer.get("status") or ""),
    }
    path = _ba_home() / "switch_journal.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def selftest() -> int:
    _read_pointer()
    _backend_port()
    return 0


def run() -> int:
    last_seen: tuple[str, str] = ("", "")
    switching_since = 0.0
    while True:
        pointer = _read_pointer()
        status = str(pointer.get("status") or "")
        key = (str(pointer.get("request_id") or ""), status)
        if key != last_seen and status:
            _journal("pointer_changed", pointer)
            last_seen = key
            switching_since = time.time() if status == "switching" else 0.0
        if status == "switching" and switching_since and time.time() - switching_since > STUCK_SWITCH_SECONDS:
            if not _backend_healthy():
                pointer["status"] = "failed"
                pointer["error"] = "switch stalled: launcher did not complete it"
                pointer["updated_at"] = time.time()
                _write_pointer(pointer)
                _journal("switch_stalled", pointer)
            switching_since = 0.0
        time.sleep(POLL_SECONDS)


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    return run()


if __name__ == "__main__":
    sys.exit(main())
