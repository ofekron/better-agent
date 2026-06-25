"""Regression test for agy (antigravity) crash recovery.

The bug: `_provider_kind` resolves agy runs to kind `"agy"`, but the recovery
replay dispatch in `_replay_and_apply` only special-cased `"gemini"` and
`"codex"`. So agy fell through to `_replay_from_claude_jsonl`, even though the
agy runner writes Gemini-shaped `session_events.jsonl`. After a backend
restart, recovered agy turns rendered empty/partial because the Claude parser
cannot read agy's events file.

The fix: agy is a gemini-family provider (provider_agy extends GeminiProvider,
writes session_events.jsonl). Recovery now routes both gemini and agy through
`_replay_from_gemini_jsonl`.

Run with:
    cd backend && .venv/bin/python scripts/test_agy_recovery.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-agy-recover-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from ingestion_versions import (  # noqa: E402
    current_ingestion_version,
    AGY_INGESTION_VERSION,
    COPILOT_INGESTION_VERSION,
)
from run_recovery import (  # noqa: E402
    _GEMINI_FAMILY_KINDS,
    _provider_kind,
    _last_assistant,
    _replay_and_apply,
    _replay_from_claude_jsonl,
    _replay_from_gemini_jsonl,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _agy_agent_message(text: str, parent_uuid: str) -> dict:
    """One agy session_events.jsonl envelope, shaped exactly like
    runner_agy._agent_message produces."""
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "agy",
            },
            "uuid": str(uuid.uuid4()),
            "parentUuid": parent_uuid,
            "timestamp": "2026-06-26T00:00:00",
            "parent_tool_use_id": None,
        },
    }


def _seed_agy_run(*, app_sid: str, agy_sid: str, events: list[dict]) -> str:
    """Synthesize an agy run dir: gemini-shaped session_events.jsonl +
    backend_state.json stamped with provider_kind=agy. Deliberately NO
    Claude state (jsonl_path / pre_query_byte_offset) so the only reader
    that can produce events is the gemini-family reader."""
    run_id = str(uuid.uuid4())
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    events_path = run_dir / "session_events.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    state = {
        "run_id": run_id,
        "mode": "native",
        "app_session_id": app_sid,
        "session_id": agy_sid,
        "complete": True,
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": app_sid,
        "mode": "native",
        "session_id": agy_sid,
        "provider_kind": "agy",
        "provider_id": "agy-test",
        "target_message_id": None,
    }), encoding="utf-8")
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": agy_sid, "error": None, "token_usage": None,
    }), encoding="utf-8")
    return run_id


def _seed_session_with_streaming_assistant() -> tuple[str, str]:
    sess = session_manager.create(
        name="t", model="agy", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "do a thing",
        "events": [], "isStreaming": False,
    })
    asst_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": asst_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": True,
    })
    return sid, asst_id


def test_agy_is_gemini_family() -> bool:
    """agy must resolve as a gemini-family kind so recovery picks the
    session_events.jsonl reader."""
    desc = {"provider_kind": "agy"}
    if _provider_kind(desc) != "agy":
        print(f"  expected kind 'agy', got {_provider_kind(desc)!r}")
        return False
    if "agy" not in _GEMINI_FAMILY_KINDS:
        print("  'agy' not in _GEMINI_FAMILY_KINDS")
        return False
    return True


def test_claude_parser_cannot_read_agy_events() -> bool:
    """Documents the bug: the Claude parser yields nothing for an agy
    session_events.jsonl, while the gemini reader yields the events."""
    app_sid, _ = _seed_session_with_streaming_assistant()
    agy_sid = str(uuid.uuid4())
    events = [_agy_agent_message("Hello", agy_sid), _agy_agent_message("world", agy_sid)]
    run_id = _seed_agy_run(app_sid=app_sid, agy_sid=agy_sid, events=events)
    run_dir = runs_root() / run_id

    via_claude = _replay_from_claude_jsonl(run_dir, unmatched_out=[])
    if via_claude:
        print(f"  Claude parser unexpectedly read agy events: {len(via_claude)}")
        return False
    via_gemini = _replay_from_gemini_jsonl(run_dir)
    if len(via_gemini) != len(events):
        print(f"  gemini reader expected {len(events)} events, got {len(via_gemini)}")
        return False
    return True


def test_agy_replay_lands_events_on_assistant() -> bool:
    """The real regression: _replay_and_apply must route agy through the
    gemini reader and land its events on the assistant message. Before the
    fix it routed to the Claude parser and the message stayed empty."""
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    agy_sid = str(uuid.uuid4())
    events = [_agy_agent_message("Hello", agy_sid), _agy_agent_message("world", agy_sid)]
    run_id = _seed_agy_run(app_sid=app_sid, agy_sid=agy_sid, events=events)

    sess = session_manager.get(app_sid)
    last_asst = _last_assistant(sess)
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid=agy_sid,
        sess=sess,
        last_asst=last_asst,
        msg_id=last_asst["id"],
    )

    sess = session_manager.get(app_sid)
    asst = next((m for m in sess["messages"] if m["id"] == asst_id), None)
    if asst is None:
        print("  assistant message disappeared")
        return False
    evs = asst.get("events") or []
    if len(evs) != len(events):
        print(f"  expected {len(events)} events on assistant, got {len(evs)}")
        return False
    for e in evs:
        if e.get("type") != "agent_message":
            print(f"  expected agent_message envelope, got {e.get('type')!r}")
            return False
    if "world" not in (asst.get("content") or ""):
        print(f"  expected replayed text in content, got {asst.get('content')!r}")
        return False
    return True


def test_agy_ingestion_version_forces_redigest() -> bool:
    """agy must have its own bumped ingestion version so runs reconciled
    under the broken v1 (Claude-parser) path re-digest on next startup."""
    v = current_ingestion_version("agy")
    if v != AGY_INGESTION_VERSION:
        print(f"  current_ingestion_version('agy')={v} != AGY_INGESTION_VERSION={AGY_INGESTION_VERSION}")
        return False
    if v <= 1:
        print(f"  agy ingestion version must be > 1 to force re-digest, got {v}")
        return False
    return True


def test_copilot_gemini_family_parity() -> bool:
    """copilot is the same kind of provider (GeminiProvider subclass writing
    session_events.jsonl) and must share the same recovery routing + bumped
    ingestion version, or it carries the identical empty-render recovery bug."""
    if "copilot" not in _GEMINI_FAMILY_KINDS:
        print("  'copilot' not in _GEMINI_FAMILY_KINDS")
        return False
    v = current_ingestion_version("copilot")
    if v != COPILOT_INGESTION_VERSION or v <= 1:
        print(f"  copilot ingestion version must be >1 and == COPILOT_INGESTION_VERSION, got {v}")
        return False
    return True


def main() -> int:
    tests = [
        test_agy_is_gemini_family,
        test_claude_parser_cannot_read_agy_events,
        test_agy_replay_lands_events_on_assistant,
        test_agy_ingestion_version_forces_redigest,
        test_copilot_gemini_family_parity,
    ]
    ok = True
    for t in tests:
        try:
            result = t()
        except Exception as exc:
            print(f"{FAIL} {t.__name__}: {type(exc).__name__}: {exc}")
            ok = False
            continue
        print(f"{PASS if result else FAIL} {t.__name__}")
        ok = ok and result
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
