"""Locks the counts-only native-import preview (`count_native_sessions`)
and the `hydrate=False` enumeration fast path.

Regression target: the settings panel used to fetch one row per native
session (a 281 MB payload across a full Claude+Codex history). That huge
response failed in the browser, leaving the preview empty, which the UI
mistook for "all imported". The fix replaces the row dump with grouped
counts and lets enumeration skip per-jsonl `cwd` reads.

Both asserted symbols are new — this test fails before the fix
(`count_native_sessions` absent, `hydrate` kwarg unknown) and passes after.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_summary.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-summary-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import  # noqa: E402
logging.getLogger(native_import.__name__).setLevel(logging.CRITICAL)
logging.getLogger("config_store").setLevel(logging.CRITICAL)
logging.getLogger("keyring").setLevel(logging.CRITICAL)
import config_store  # noqa: E402

CASES = {"n": 0}


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    CASES["n"] += 1


def _make_claude_layout(root: Path, sids: list[str], cwd: str) -> None:
    """One project dir holding `sids` jsonl transcripts. Each line carries a
    `cwd` field so the hydrate path has something to read."""
    d = root / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    for sid in sids:
        (d / f"{sid}.jsonl").write_text(
            json.dumps({
                "type": "user", "uuid": str(uuid.uuid4()), "cwd": cwd,
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                "timestamp": "2026-01-01T00:00:00Z",
            }) + "\n" +
            json.dumps({
                "type": "assistant", "uuid": str(uuid.uuid4()), "cwd": cwd,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi reply"}]},
                "timestamp": "2026-01-01T00:00:01Z",
            }) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    home = Path(_TMP_HOME) / "claude-home"
    sids = ["s1", "s2", "s3", "s4"]
    _make_claude_layout(home, sids, cwd="/work/proj")

    prov = config_store.add_provider({
        "name": "summary-test", "kind": "claude", "mode": "subscription",
        "config_dir": str(home),
    })
    pid = prov["id"]
    try:
        # Mark two of the four as already imported.
        native_import._registry_set("claude:s1", str(uuid.uuid4()))
        native_import._registry_set("claude:s2", str(uuid.uuid4()))

        summary = native_import.count_native_sessions([pid])
        check(summary["total"] == 4, f"total {summary['total']} != 4")
        check(summary["imported"] == 2, f"imported {summary['imported']} != 2")
        check(summary["pending"] == 2, f"pending {summary['pending']} != 2")
        check(summary["pending"] == summary["total"] - summary["imported"],
              "pending must equal total - imported")

        claude = summary["by_provider"].get("claude")
        check(claude is not None, "by_provider missing claude group")
        check(claude == {"total": 4, "imported": 2, "pending": 2},
              f"claude group {claude} wrong")

        # The summary must be counts, not rows — no per-session list leaks out.
        check("sessions" not in summary, "summary must not embed a sessions list")

        # hydrate=False skips the per-jsonl cwd read; hydrate=True populates it.
        lite = native_import.enumerate_native_sessions([pid], hydrate=False)
        full = native_import.enumerate_native_sessions([pid], hydrate=True)
        check(len(lite) == 4 and len(full) == 4, "both enumerations see all 4")
        check(all(s.cwd == "" for s in lite), "hydrate=False must leave cwd empty")
        check(all(s.cwd == "/work/proj" for s in full), "hydrate=True must read cwd")
        # Identity fields survive regardless of hydrate.
        check({s.native_id for s in lite} == set(sids), "native_id present without hydrate")
    finally:
        config_store.delete_provider(pid)

    print(f"OK — {CASES['n']} checks passed")


if __name__ == "__main__":
    main()
