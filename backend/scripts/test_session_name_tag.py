"""Regression tests for the agent-proposed <SESSION_NAME> rename tag.

Pins:
  1. `file_ref_resolver.extract_session_name` extracts the inner text of a
     complete <SESSION_NAME>…</SESSION_NAME> pair and returns None for
     missing / incomplete / empty tags.
  2. `strip_session_name_tag` removes the whole tag (wrapper + inner text)
     from rendered prose; `_apply_tag_rules` applies the strip even when no
     extension tag rules are registered.
  3. Live apply_event (source_is_provider_stream=True) on an agent_message
     carrying the tag renames the session even with the default
     agent_rename_allowed=False (explicit tag bypasses the ai-title gate).
  4. Re-applying the same tag is change-gated (no-op when the name matches).
  5. prepare_provider_event_for_journal renames from the RAW text and the
     journal-prepared data no longer contains the tag.
  6. ai-title metadata stays gated on agent_rename_allowed.

Run with:
    cd backend && .venv/bin/python scripts/test_session_name_tag.py
"""

from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-name-tag-")

import file_ref_resolver  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session() -> tuple[str, dict]:
    sess = session_manager.create(
        name="✏️ Edit — foo.py", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    return sid, scaffold


def _agent_message(uuid: str, text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def test_extract_and_strip() -> bool:
    ok = True
    ex = file_ref_resolver.extract_session_name
    st = file_ref_resolver.strip_session_name_tag
    ok &= ex("a <SESSION_NAME>✏️ Fix auth</SESSION_NAME> b") == "✏️ Fix auth"
    ok &= ex("no tag here") is None
    ok &= ex("<SESSION_NAME>unterminated") is None
    ok &= ex("<SESSION_NAME>  </SESSION_NAME>") is None
    ok &= st("a <SESSION_NAME>✏️ Fix auth</SESSION_NAME>\nb") == "a b"
    ok &= st("plain") == "plain"
    # _apply_tag_rules strips it even with zero extension rules registered.
    file_ref_resolver.set_tag_rules([])
    ok &= file_ref_resolver._apply_tag_rules(
        "x <SESSION_NAME>n</SESSION_NAME> y"
    ) == "x  y"
    return bool(ok)


def test_apply_event_renames_without_allowed_flag() -> bool:
    sid, msg = _mk_session()
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r1")
    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=_agent_message("u1", "Got it. <SESSION_NAME>✏️ Rework parser</SESSION_NAME>"),
        ctx=ctx,
        source_is_provider_stream=True,
        write_journal=False,
    )
    sess = session_manager.get_lite(sid) or {}
    if sess.get("name") != "✏️ Rework parser":
        print(f"  expected rename, got name={sess.get('name')!r}")
        return False
    if sess.get("agent_rename_allowed"):
        print("  fixture invalid: agent_rename_allowed unexpectedly True")
        return False
    # Change-gate: same tag again (new uuid) is a no-op.
    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=_agent_message("u2", "<SESSION_NAME>✏️ Rework parser</SESSION_NAME>"),
        ctx=ctx,
        source_is_provider_stream=True,
        write_journal=False,
    )
    sess = session_manager.get_lite(sid) or {}
    return sess.get("name") == "✏️ Rework parser"


def test_prepare_for_journal_renames_and_strips() -> bool:
    sid, _msg = _mk_session()
    strategy = get_strategy("native")
    etype, data = strategy.prepare_provider_event_for_journal(
        app_session_id=sid,
        event=_agent_message("u3", "Plan:\n<SESSION_NAME>✏️ Split config</SESSION_NAME>\ndone"),
    )
    sess = session_manager.get_lite(sid) or {}
    if sess.get("name") != "✏️ Split config":
        print(f"  expected rename via prepare, got name={sess.get('name')!r}")
        return False
    text = data["message"]["content"][0]["text"]
    if "<SESSION_NAME>" in text or "✏️ Split config" in text:
        print(f"  tag not stripped from journal data: {text!r}")
        return False
    return etype == "agent_message"


def test_ai_title_still_gated() -> bool:
    sid, _msg = _mk_session()
    strategy = get_strategy("native")
    strategy._apply_metadata_side_effects(
        app_session_id=sid, data={"type": "ai-title", "aiTitle": "auto name"},
    )
    sess = session_manager.get_lite(sid) or {}
    if sess.get("name") == "auto name":
        print("  ai-title renamed despite agent_rename_allowed=False")
        return False
    session_manager.set_agent_rename_allowed(sid, True)
    strategy._apply_metadata_side_effects(
        app_session_id=sid, data={"type": "ai-title", "aiTitle": "auto name"},
    )
    sess = session_manager.get_lite(sid) or {}
    return sess.get("name") == "auto name"


def main() -> int:
    tests = [
        test_extract_and_strip,
        test_apply_event_renames_without_allowed_flag,
        test_prepare_for_journal_renames_and_strips,
        test_ai_title_still_gated,
    ]
    failures = 0
    for t in tests:
        try:
            ok = t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok = False
            print(f"  raised: {exc}")
        print(f"{PASS if ok else FAIL} {t.__name__}")
        failures += 0 if ok else 1
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
