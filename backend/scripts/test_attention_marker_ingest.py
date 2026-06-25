"""Regression: attention-marker tags set the session marker on LIVE ingest.

The bug: `apply_event` strips the `<TAG>...</TAG>` wrapper out of msg.events
during the live file-ref/tag rewrite, so the old turn-complete watcher (which
re-scanned the already-stripped render tree) never matched and the marker was
never set — for BOTH NEEDS_USER_DECISION and ALL_TASKS__DONE.

The fix detects markers on the RAW assistant text inside apply_event, before
the strip, and sets the marker (change-gated). This test feeds a live
agent_message whose text carries the tag and asserts the marker lands.

Run with:
    cd backend && .venv/bin/python scripts/test_attention_marker_ingest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-attn-marker-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402
import extension_applied_config  # noqa: E402
import file_ref_resolver  # noqa: E402
import session_store  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _load_real_rules() -> None:
    man = json.loads(
        (Path(_BACKEND).parent / "extensions" / "user-attention"
         / "better-agent-extension.json").read_text(encoding="utf-8")
    )
    rec = {"enabled": True, "manifest": extension_store.validate_manifest(man)}
    extension_applied_config._all_enabled_records = lambda: [rec]  # type: ignore
    extension_applied_config.reconcile_all()


def _mk_session() -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    session_manager.append_assistant_msg(sid, strategy.build_assistant_scaffold())
    msg = next(m for m in session_manager.get(sid)["messages"]
               if m["role"] == "assistant")
    return sid, msg


def _live_agent_message(uuid: str, text: str) -> dict:
    return {
        "type": "manager_event",
        "data": {"event": {
            "type": "agent_message",
            "data": {
                "uuid": uuid, "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            },
        }},
    }


def _apply(sid: str, msg: dict, event: dict) -> None:
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=event,
                         ctx=ctx, source_is_provider_stream=True)


def main() -> int:
    try:
        _load_real_rules()
        assert {"NEEDS_USER_DECISION", "ALL_TASKS__DONE"} <= set(
            file_ref_resolver.tag_names()), file_ref_resolver.tag_names()

        # 1) Blue dot — ALL_TASKS__DONE.
        sid, msg = _mk_session()
        _apply(sid, msg, _live_agent_message(
            "u1", "Done.\n<ALL_TASKS__DONE>All set.</ALL_TASKS__DONE>"))
        markers = session_store._markers_for_session(sid)
        assert markers.get("ofek-dev.user-attention", {}).get("color") == "#2563eb", \
            f"blue marker not set: {markers}"

        # The wrapper is still stripped from the render tree (render unchanged).
        fresh = session_manager.get(sid)
        asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
        rendered = "".join(
            b.get("text", "")
            for ev in asst["events"]
            for b in ((ev.get("data") or {}).get("message") or {}).get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "<ALL_TASKS__DONE>" not in rendered, rendered

        # 2) Orange dot — NEEDS_USER_DECISION.
        sid2, msg2 = _mk_session()
        _apply(sid2, msg2, _live_agent_message(
            "u2", "<NEEDS_USER_DECISION>Pick A or B</NEEDS_USER_DECISION>"))
        m2 = session_store._markers_for_session(sid2)
        assert m2.get("ofek-dev.user-attention", {}).get("color") == "#ff8c00", \
            f"orange marker not set: {m2}"

        # 3) Change-gate: re-applying the same streaming uuid does not re-fire.
        fires: list[tuple] = []
        orig_fire = session_manager._fire
        session_manager._fire = lambda *a, **k: fires.append((a, k))  # type: ignore
        try:
            _apply(sid, msg, _live_agent_message(
                "u1", "Done.\n<ALL_TASKS__DONE>All set.</ALL_TASKS__DONE>"))
        finally:
            session_manager._fire = orig_fire  # type: ignore
        marker_fires = [f for f in fires
                        if f[0][1:] and isinstance(f[0][1], dict)
                        and f[0][1].get("kind") == "marker_set"]
        assert not marker_fires, f"marker re-fired on identical re-apply: {marker_fires}"

        print("PASS test_attention_marker_ingest")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
