"""Locks render-time panel-anchor derivation in render_stub.timeline_events.

Regression for: a `create_sub_session -> ask` sub-session event group
rendered BEFORE `create_sub_session` because the backend-stamped
`insert_at` is captured synchronously at MCP-tool-fire time, before the
triggering tool_use event has been tail-appended to the message. The
renderer now derives each panel's anchor from the actual tool_use entry
position, so the panel lands AFTER its tool call.

Run: python3 backend/scripts/test_panel_anchor_ordering.py
"""

import os
import sys
import tempfile

os.environ.setdefault("BETTER_AGENT_HOME", tempfile.mkdtemp(prefix="bc_anchor_test_"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import render_stub


def _assistant_tool_use(*tools):
    """One assistant event entry holding the given (name, id) tool_use blocks."""
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": name, "id": tid, "input": {}}
                    for name, tid in tools
                ]
            },
        },
    }


def _text(label):
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": label}]},
        },
    }


def _tool_result(label):
    return {
        "type": "agent_message",
        "data": {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": label}]},
        },
    }


def _worker_event(label):
    return {"type": "agent_message",
            "data": {"type": "assistant",
                     "message": {"content": [{"type": "text", "text": label}]}}}


def _text_of(ev):
    try:
        return ev["data"]["message"]["content"][0].get("text") \
            or ev["data"]["message"]["content"][0].get("content")
    except Exception:
        return None


def test_create_sub_session_then_ask_orders_panel_after_ask():
    msg = {
        "events": [
            _text("creating"),                                            # 0
            _assistant_tool_use(("mcp__handoff__create_sub_session", "c1")),  # 1
            _tool_result("created ok"),                                   # 2
            _assistant_tool_use(("mcp__communicate__ask", "a1")),         # 3
            _tool_result("ask ok"),                                       # 4
        ],
        "workers": [
            # Both panels carry the racy/wrong stored insert_at=0 (would put
            # them at the very front under the old behavior).
            {"delegation_id": "created_sub", "panel_kind": "sub_session_created",
             "run_mode": "created", "insert_at": 0, "events": []},
            {"delegation_id": "team_ask_1", "panel_kind": "sub_session",
             "run_mode": "team_ask", "insert_at": 0,
             "events": [_worker_event("SUBAGENT_WORK")]},
        ],
    }
    out = render_stub.timeline_events(msg)
    labels = [_text_of(e) for e in out]
    sub = labels.index("SUBAGENT_WORK")
    # The sub-session group must land AFTER both the create call and the ask call.
    assert sub > labels.index("creating"), labels
    assert "created ok" in labels[:sub], labels       # after create_sub_session+result
    assert "ask ok" not in labels[:sub], labels       # before the ask's own result
    # Concretely: text, create(c1 tool_use), created-result, ask(a1 tool_use),
    # SUBAGENT_WORK, ask-result
    assert labels == [
        "creating", None, "created ok", None, "SUBAGENT_WORK", "ask ok",
    ], labels
    print("ok: create_sub_session -> ask places panel after the ask")


def test_parallel_asks_share_entry_keep_firing_order():
    # Two asks to already-known sids in ONE assistant message = one entry.
    msg = {
        "events": [
            _assistant_tool_use(
                ("mcp__communicate__ask", "a1"),
                ("mcp__communicate__ask", "a2"),
            ),  # 0
        ],
        "workers": [
            {"delegation_id": "ask_1", "panel_kind": "sub_session",
             "run_mode": "team_ask", "insert_at": 0,
             "events": [_worker_event("ASK_ONE")]},
            {"delegation_id": "ask_2", "panel_kind": "sub_session",
             "run_mode": "team_ask", "insert_at": 0,
             "events": [_worker_event("ASK_TWO")]},
        ],
    }
    labels = [_text_of(e) for e in render_stub.timeline_events(msg)]
    # Both anchor right after entry 0; firing order preserved.
    assert labels == [None, "ASK_ONE", "ASK_TWO"], labels
    print("ok: parallel asks in one entry keep firing order after the entry")


def test_async_anchors_like_mssg():
    msg = {
        "events": [
            _text("before"),
            _assistant_tool_use(("mcp__communicate__async", "ac1")),
            _tool_result("queued"),
        ],
        "workers": [
            {"delegation_id": "async_1", "panel_kind": "sub_session",
             "run_mode": "team_message", "insert_at": 0,
             "events": [_worker_event("ASYNC_WORK")]},
        ],
    }
    labels = [_text_of(e) for e in render_stub.timeline_events(msg)]
    assert labels == ["before", None, "ASYNC_WORK", "queued"], labels
    print("ok: async panel anchors after the tool call")


def test_panel_without_tool_use_falls_back_to_stored_insert_at():
    # A Codex-native-style panel with no MCP tool_use in the stream: keeps
    # its stored insert_at instead of being dropped or crashing.
    msg = {
        "events": [_text("a"), _text("b"), _text("c")],
        "workers": [
            {"delegation_id": "native_1", "panel_kind": "worker",
             "run_mode": "native", "insert_at": 2,
             "events": [_worker_event("NATIVE_WORK")]},
        ],
    }
    labels = [_text_of(e) for e in render_stub.timeline_events(msg)]
    assert labels == ["a", "b", "NATIVE_WORK", "c"], labels
    print("ok: unmatched panel falls back to stored insert_at")


def test_create_worker_tool_use_is_ignored_does_not_desync_ask():
    # create_worker is approval-gated; its worker panel appears later via a
    # separate delegation, so its tool_use must NOT consume the ask panel.
    msg = {
        "events": [
            _assistant_tool_use(("mcp__communicate__create_worker", "w1")),  # 0
            _assistant_tool_use(("mcp__communicate__ask", "a1")),            # 1
        ],
        "workers": [
            {"delegation_id": "team_ask_1", "panel_kind": "sub_session",
             "run_mode": "team_ask", "insert_at": 0,
             "events": [_worker_event("SUBAGENT_WORK")]},
        ],
    }
    labels = [_text_of(e) for e in render_stub.timeline_events(msg)]
    # SUBAGENT_WORK anchors after the ask entry (idx 1), not the create_worker entry.
    assert labels == [None, None, "SUBAGENT_WORK"], labels
    print("ok: create_worker tool_use ignored; ask panel still anchors after ask")


if __name__ == "__main__":
    test_create_sub_session_then_ask_orders_panel_after_ask()
    test_parallel_asks_share_entry_keep_firing_order()
    test_async_anchors_like_mssg()
    test_panel_without_tool_use_falls_back_to_stored_insert_at()
    test_create_worker_tool_use_is_ignored_does_not_desync_ask()
    print("ALL PASS")
