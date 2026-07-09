"""Regression tests for Codex native metadata filtering.

Run with:
    cd backend && .venv/bin/python scripts/test_codex_metadata_filtering.py
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-codex-metadata-")

from codex_native import CodexRolloutNormalizer  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _normalize(event: dict) -> list[dict]:
    return CodexRolloutNormalizer(namespace="metadata-test").normalize_line(
        json.dumps(event)
    )


def _assistant_text(event: dict) -> str:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list) or not content:
        return ""
    block = content[0]
    if not isinstance(block, dict):
        return ""
    return str(block.get("text") or "")


def test_thread_settings_applied_is_filtered() -> bool:
    rows = _normalize({
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-5.5",
                "cwd": "/Users/ofekron/better-claude",
                "approval_policy": "never",
            },
        },
    })
    if rows:
        print(f"  expected no render rows, got {rows!r}")
        return False
    return True


def test_world_state_is_filtered() -> bool:
    rows = _normalize({
        "type": "world_state",
        "payload": {
            "type": "world_state",
            "full": False,
            "state": {"agents_md": {"text": "rules"}},
        },
    })
    if rows:
        print(f"  expected no render rows, got {rows!r}")
        return False
    return True


def test_unknown_native_event_still_renders_debug_card() -> bool:
    rows = _normalize({
        "type": "event_msg",
        "payload": {"type": "new_visible_event", "value": 1},
    })
    if len(rows) != 1:
        print(f"  expected one debug render row, got {rows!r}")
        return False
    text = _assistant_text(rows[0])
    if "Codex native event_msg.new_visible_event" not in text:
        print(f"  expected native debug text, got {text!r}")
        return False
    return True


TESTS = [
    ("thread_settings_applied is filtered", test_thread_settings_applied_is_filtered),
    ("world_state is filtered", test_world_state_is_filtered),
    ("unknown native event still renders debug card", test_unknown_native_event_still_renders_debug_card),
]


def main() -> int:
    failed = False
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"  exception: {exc!r}")
        print(f"{PASS if ok else FAIL} {name}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
