"""Unit tests for the pure render-tree stubbing helpers in render_stub.py.

These cover the panel-anchor / delegation-interleaving machinery, the
in-place stubbing mutators, content materialization, and the
latest-assistant-id selector — all pure dict-in/dict-out logic that the
existing integration-style stub tests only reach indirectly. No backend
process, no session state; render_stub is imported directly.

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python -m pytest scripts/test_render_stub_unit.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import render_stub  # noqa: E402


# --- event builders -------------------------------------------------------

def _text_event(uid: str, text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {"type": "assistant", "uuid": uid, "message": {"content": text}},
    }


def _assistant_tool_use_event(uid: str, tool_name: str) -> dict:
    """An agent_message/assistant event carrying one tool_use block — the
    shape `_delegation_tool_uses` scans for."""
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": uid,
            "message": {"content": [{"type": "tool_use", "name": tool_name}]},
        },
    }


def _worker(delegation_id: str, events=None, **extra) -> dict:
    w = {"delegation_id": delegation_id, "events": events or []}
    w.update(extra)
    return w


# --- _tool_short_name -----------------------------------------------------

def test_tool_short_name_no_double_underscore_returns_whole():
    assert render_stub._tool_short_name("ask") == "ask"


def test_tool_short_name_suffix_after_last_double_underscore():
    assert render_stub._tool_short_name("mcp__ask") == "ask"
    # only the last separator splits
    assert render_stub._tool_short_name("ns__sub__mssg") == "mssg"


# --- _delegation_tool_uses -----------------------------------------------

def test_delegation_tool_uses_skips_non_agent_message():
    evs = [
        {"type": "manager_event"},          # not agent_message
        {"type": "agent_message", "data": {"type": "user"}},  # data not assistant
        42,                                  # not a dict
    ]
    assert render_stub._delegation_tool_uses(evs) == []


def test_delegation_tool_uses_skips_non_assistant_data_and_bad_message():
    evs = [
        {"type": "agent_message", "data": "nope"},               # data not dict
        {"type": "agent_message", "data": {"type": "assistant"}},  # message missing -> content None
        {"type": "agent_message", "data": {"type": "assistant", "message": "x"}},  # message not dict
        {"type": "agent_message", "data": {"type": "assistant", "message": {"content": "str"}}},  # content not list
    ]
    assert render_stub._delegation_tool_uses(evs) == []


def test_delegation_tool_uses_skips_non_tool_use_and_unnamed_blocks():
    evs = [
        {"type": "agent_message", "data": {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"},          # not tool_use
            {"type": "tool_use"},                    # no name
            {"type": "tool_use", "name": 123},       # name not str
        ]}}},
    ]
    assert render_stub._delegation_tool_uses(evs) == []


def test_delegation_tool_uses_excludes_create_worker_collects_delegations():
    evs = [
        _assistant_tool_use_event("u0", "mcp__create_worker"),  # excluded short name
        _assistant_tool_use_event("u1", "mcp__ask"),
        _assistant_tool_use_event("u2", "ns__delegate_task"),
    ]
    out = render_stub._delegation_tool_uses(evs)
    assert out == [(1, "ask"), (2, "delegate_task")]


def test_delegation_tool_uses_parallel_blocks_share_entry_index():
    # two delegation tool_use blocks inside one assistant entry share index
    evs = [{
        "type": "agent_message",
        "data": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__ask"},
            {"type": "tool_use", "name": "mcp__mssg"},
        ]}},
    }]
    assert render_stub._delegation_tool_uses(evs) == [(0, "ask"), (0, "mssg")]


# --- _panel_matches_tool --------------------------------------------------

def test_panel_matches_tool_create_sub_session():
    assert render_stub._panel_matches_tool("create_sub_session", {"panel_kind": "sub_session_created"}) is True
    assert render_stub._panel_matches_tool("create_sub_session", {"panel_kind": "other"}) is False


def test_panel_matches_tool_create_session():
    assert render_stub._panel_matches_tool("create_session", {"panel_kind": "session_created"}) is True
    assert render_stub._panel_matches_tool("create_session", {"panel_kind": "other"}) is False


def test_panel_matches_tool_ask_run_modes():
    for rm in ("team_ask", "fork", "team_message"):
        assert render_stub._panel_matches_tool("ask", {"run_mode": rm}) is True
    assert render_stub._panel_matches_tool("ask", {"run_mode": "native"}) is False


def test_panel_matches_tool_mssg_and_delegate_task():
    for short in ("mssg", "delegate_task"):
        assert render_stub._panel_matches_tool(short, {"run_mode": "team_message"}) is True
        assert render_stub._panel_matches_tool(short, {"run_mode": "fork"}) is False


def test_panel_matches_tool_unknown_short_returns_false():
    assert render_stub._panel_matches_tool("something_else", {}) is False


# --- _derive_panel_anchors ------------------------------------------------

def test_derive_panel_anchors_consumes_tool_uses_positionally():
    manager = [_text_event("t0", "a"), _assistant_tool_use_event("u1", "mcp__ask"),
               _text_event("t1", "b"), _assistant_tool_use_event("u2", "mcp__mssg")]
    workers = [
        _worker("d1", run_mode="team_ask"),
        _worker("d2", run_mode="team_message"),
    ]
    # ask tool_use at entry 1 -> anchor 2; mssg tool_use at entry 3 -> anchor 4
    anchors = render_stub._derive_panel_anchors(manager, [(workers[0], 0), (workers[1], 1)])
    assert anchors == {"d1": 2, "d2": 4}


def test_derive_panel_anchors_skips_non_matching_worker_without_advancing_cursor():
    manager = [_assistant_tool_use_event("u1", "mcp__ask")]
    workers = [
        _worker("d1", run_mode="native"),   # does not match ask -> cursor stays
        _worker("d2", run_mode="team_ask"),  # matches -> consumes the ask
    ]
    anchors = render_stub._derive_panel_anchors(manager, [(workers[0], 0), (workers[1], 1)])
    assert anchors == {"d2": 1}


def test_derive_panel_anchors_cursor_exhaustion_leaves_unmatched_unanchored():
    manager = [_assistant_tool_use_event("u1", "mcp__ask")]
    workers = [
        _worker("d1", run_mode="team_ask"),
        _worker("d2", run_mode="team_ask"),  # no second tool_use to consume
    ]
    anchors = render_stub._derive_panel_anchors(manager, [(workers[0], 0), (workers[1], 1)])
    assert anchors == {"d1": 1}


# --- _panel_anchors cache -------------------------------------------------

def test_panel_anchors_caches_after_first_derive():
    manager = [_assistant_tool_use_event("u1", "mcp__ask")]
    workers = [(_worker("d1", run_mode="team_ask"), 0)]
    msg = {"events": manager}
    first = render_stub._panel_anchors(msg, manager, workers)
    assert first == {"d1": 1}
    cached_blob = msg[render_stub._PANEL_ANCHOR_CACHE]
    assert isinstance(cached_blob, dict) and cached_blob["key"] is not None
    # second call with identical inputs hits the cache path (returns same object)
    second = render_stub._panel_anchors(msg, manager, workers)
    assert second == first


def test_panel_anchors_rederives_when_worker_key_changes():
    manager = [_assistant_tool_use_event("u1", "mcp__ask")]
    msg = {"events": manager}
    w1 = [(_worker("d1", run_mode="team_ask"), 0)]
    render_stub._panel_anchors(msg, manager, w1)
    # change worker identity -> key mismatches -> re-derive, no stale return
    w2 = [(_worker("d2", run_mode="team_ask"), 0)]
    out = render_stub._panel_anchors(msg, manager, w2)
    assert out == {"d2": 1}


def test_panel_anchors_rederives_when_cache_anchors_corrupted():
    manager = [_assistant_tool_use_event("u1", "mcp__ask")]
    workers = [(_worker("d1", run_mode="team_ask"), 0)]
    key = render_stub._panel_anchor_cache_key(manager, workers)
    # matching key but anchors is not a dict -> must fall through and re-derive
    msg = {"events": manager, render_stub._PANEL_ANCHOR_CACHE: {"key": key, "anchors": None}}
    assert render_stub._panel_anchors(msg, manager, workers) == {"d1": 1}
    assert msg[render_stub._PANEL_ANCHOR_CACHE]["anchors"] == {"d1": 1}


def test_invalidate_panel_anchor_cache_drops_blob():
    msg = {render_stub._PANEL_ANCHOR_CACHE: {"key": "x"}}
    render_stub.invalidate_panel_anchor_cache(msg)
    assert render_stub._PANEL_ANCHOR_CACHE not in msg


# --- timeline_events (delegation interleaving) ----------------------------

def test_timeline_events_no_workers_returns_renderable_primary():
    msg = {"events": [_text_event("a", "hi"), {"type": "complete"}]}
    # 'complete' is a non-render type -> dropped
    out = render_stub.timeline_events(msg)
    assert [e["data"]["uuid"] for e in out] == ["a"]


def test_timeline_events_skips_non_dict_and_duplicate_delegation_workers():
    msg = {
        "events": [_text_event("a", "hi")],
        "workers": [
            "notadict",                               # non-dict -> skipped
            _worker("d1", events=[_text_event("w1", "woof")]),
            _worker("d1", events=[_text_event("dup", "x")]),  # duplicate id -> skipped
        ],
    }
    out = render_stub.timeline_events(msg)
    uuids = [e["data"]["uuid"] for e in out]
    assert uuids == ["a", "w1"]


def test_timeline_events_interleaves_worker_at_derived_anchor():
    manager = [
        _text_event("m0", "zero"),
        _assistant_tool_use_event("tu", "mcp__ask"),   # anchor point
        _text_event("m1", "one"),
    ]
    msg = {
        "events": manager,
        "workers": [_worker("d1", run_mode="team_ask", events=[_text_event("w", "worker")])],
    }
    uuids = [e["data"]["uuid"] for e in render_stub.timeline_events(msg)]
    # primary[0:2] (m0, tu), worker (w), primary[2:] (m1)
    assert uuids == ["m0", "tu", "w", "m1"]


def test_timeline_events_falls_back_to_stored_insert_at():
    manager = [_text_event("m0", "zero"), _text_event("m1", "one"), _text_event("m2", "two")]
    # no matching tool_use -> derived anchor absent -> stored insert_at used
    msg = {
        "events": manager,
        "workers": [_worker("d1", run_mode="native", insert_at=1, events=[_text_event("w", "worker")])],
    }
    uuids = [e["data"]["uuid"] for e in render_stub.timeline_events(msg)]
    # primary[0:1] (m0), worker (w), primary[1:] (m1, m2)
    assert uuids == ["m0", "w", "m1", "m2"]


def test_timeline_events_inf_anchor_appends_worker_before_remaining_primary():
    manager = [_text_event("m0", "zero"), _text_event("m1", "one")]
    # no derived anchor, no insert_at -> inf -> stop = len(manager)
    msg = {
        "events": manager,
        "workers": [_worker("d1", run_mode="native", events=[_text_event("w", "worker")])],
    }
    uuids = [e["data"]["uuid"] for e in render_stub.timeline_events(msg)]
    assert uuids == ["m0", "m1", "w"]


def test_renderable_count_matches_timeline_length():
    msg = {"events": [_text_event("a", "x"), {"type": "session_discovered"}], "workers": []}
    assert render_stub.renderable_count(msg) == 1


# --- stub_preview_events / build_stub -------------------------------------

def test_stub_preview_events_tail_only():
    rendered = [_text_event(str(i), str(i)) for i in range(3)]
    out = render_stub.stub_preview_events(rendered, tail=2)
    assert out == rendered[-2:]


def test_stub_preview_events_tail_zero_still_pins_boundary_markers():
    # tail=0 -> no tail, but pinned markers are independent of tail position
    steer = {"type": "steer_prompt", "data": {"uuid": "s"}}
    rendered = [_text_event("0", "0"), steer]
    assert render_stub.stub_preview_events(rendered, tail=0) == [steer]


def test_stub_preview_events_pins_boundary_markers_outside_tail():
    steer = {"type": "steer_prompt", "data": {"uuid": "s"}}
    switched = {"type": "model_switched", "data": {"uuid": "ms"}}
    tail = [_text_event(str(i), str(i)) for i in range(3)]
    rendered = [steer, switched] + tail
    out = render_stub.stub_preview_events(rendered, tail=2)
    # pinned (steer, switched, in order) then tail
    assert out[:2] == [steer, switched]
    assert out[2:] == tail[-2:]


def test_stub_preview_events_pinned_cap_keeps_most_recent():
    # 12 pinned markers, cap is 8 -> only the last 8 survive
    pinned = [{"type": "steer_prompt", "data": {"uuid": f"s{i}"}} for i in range(12)]
    tail = [_text_event("t", "t")]
    out = render_stub.stub_preview_events(pinned + tail, tail=1)
    pinned_out = [e for e in out if e.get("type") == "steer_prompt"]
    assert len(pinned_out) == render_stub._STUB_PINNED_CAP
    assert pinned_out == pinned[-render_stub._STUB_PINNED_CAP:]


def test_build_stub_counts_and_tails():
    msg = {"events": [_text_event("a", "x"), _text_event("b", "y")], "workers": []}
    stub = render_stub.build_stub(msg, tail=1)
    assert stub["event_count"] == 2
    assert [e["data"]["uuid"] for e in stub["last_events"]] == ["b"]


def test_build_stub_from_events_filters_non_render():
    evs = [_text_event("a", "x"), {"type": "complete"}]
    stub = render_stub.build_stub_from_events(evs, tail=5)
    assert stub["event_count"] == 1
    assert [e["data"]["uuid"] for e in stub["last_events"]] == ["a"]


# --- message_output_text --------------------------------------------------

def test_message_output_text_empty_is_blank():
    assert render_stub.message_output_text({"events": [], "workers": []}) == ""


def test_message_output_text_extracts_primary_text():
    msg = {"events": [_text_event("a", "hello world")], "workers": []}
    assert render_stub.message_output_text(msg) == "hello world"


def test_parent_content_projection_excludes_worker_panel_output():
    parent_event = _text_event("parent", "parent final text")
    worker_event = _text_event("worker", "worker final text")
    msg = {
        "events": [parent_event],
        "workers": [_worker("d1", events=[worker_event])],
    }

    assert render_stub.timeline_events(msg) == [parent_event, worker_event]
    assert render_stub.message_output_text(msg) == "parent final text"

    render_stub.mark_message_content_dirty(msg)
    assert render_stub.materialize_message_content(msg) == "parent final text"


# --- mark_message_content_dirty / materialize_message_content -------------

def test_materialize_returns_existing_content_when_not_dirty():
    msg = {"content": "cached", "events": [], "workers": []}
    assert render_stub.materialize_message_content(msg) == "cached"
    # no projection performed -> content untouched, no dirty flag written
    assert msg["content"] == "cached"
    assert "_content_dirty" not in msg


def test_materialize_projects_when_missing_content(monkeypatch):
    projected = {"events": [_text_event("a", "fresh")], "workers": []}
    captured = {}

    def fake_project(events, current):
        captured["events_len"] = len(events)
        captured["current"] = current
        return "projected-text"

    import event_shape
    monkeypatch.setattr(event_shape, "project_content_snapshot", fake_project)
    msg = {"events": [_text_event("a", "fresh")], "workers": []}
    out = render_stub.materialize_message_content(msg)
    assert out == "projected-text"
    assert msg["content"] == "projected-text"
    assert msg["_content_dirty"] is False
    assert captured == {"events_len": 1, "current": None}


def test_materialize_projects_when_marked_dirty(monkeypatch):
    import event_shape
    monkeypatch.setattr(event_shape, "project_content_snapshot", lambda e, c: "re-projected")
    msg = {"content": "stale", "events": [], "workers": []}
    render_stub.mark_message_content_dirty(msg)
    assert msg["_content_dirty"] is True
    assert render_stub.materialize_message_content(msg) == "re-projected"


# --- latest_assistant_id --------------------------------------------------

def test_latest_assistant_id_none_when_empty():
    assert render_stub.latest_assistant_id([]) is None


def test_latest_assistant_id_ignores_non_assistant_and_idless():
    msgs = [
        {"role": "user", "id": "u1"},
        {"role": "assistant"},                    # no id
        {"role": "tool", "id": "t1"},
    ]
    assert render_stub.latest_assistant_id(msgs) is None


def test_latest_assistant_id_picks_max_seq():
    msgs = [
        {"role": "assistant", "id": "a1", "seq": 1},
        {"role": "assistant", "id": "a2", "seq": 5},
        {"role": "assistant", "id": "a3", "seq": 3},
    ]
    assert render_stub.latest_assistant_id(msgs) == "a2"


def test_latest_assistant_id_seq_tie_last_in_order_wins():
    msgs = [
        {"role": "assistant", "id": "a1", "seq": 2},
        {"role": "assistant", "id": "a2", "seq": 2},
    ]
    assert render_stub.latest_assistant_id(msgs) == "a2"


def test_latest_assistant_id_fallback_no_seqs_keeps_first_then_unseated():
    # no seqs anywhere: first assistant kept, subsequent (seq None) cannot
    # beat latest_seq None via the `>=` guard -> first wins
    msgs = [
        {"role": "user", "id": "u"},
        {"role": "assistant", "id": "a1"},
        {"role": "assistant", "id": "a2"},
    ]
    assert render_stub.latest_assistant_id(msgs) == "a1"


def test_latest_assistant_id_unseated_lower_seq_kept_against_none():
    msgs = [
        {"role": "assistant", "id": "a1", "seq": 9},
        {"role": "assistant", "id": "a2"},  # seq None -> cannot replace (None >= 9 is False)
    ]
    assert render_stub.latest_assistant_id(msgs) == "a1"


# --- _empty_event_lists / stub_message_inplace ----------------------------

def test_empty_event_lists_clears_primary_and_caches():
    msg = {
        "events": [_text_event("a", "x")],
        "_uid_idx": {"a": 0},
        "_has_final": True,
        render_stub._PANEL_ANCHOR_CACHE: {"key": "x"},
        "workers": [
            {"events": [_text_event("w", "y")], "_uid_idx": {"w": 0}, "_has_final": False},
            "notadict",                      # skipped gracefully
            {"delegation_id": "d"},          # no events list -> no-op
        ],
    }
    render_stub._empty_event_lists(msg)
    assert msg["events"] == []
    assert "_uid_idx" not in msg and "_has_final" not in msg
    assert render_stub._PANEL_ANCHOR_CACHE not in msg
    w0 = msg["workers"][0]
    assert w0["events"] == [] and "_uid_idx" not in w0 and "_has_final" not in w0


def test_empty_event_lists_noop_when_no_events():
    msg = {"workers": []}
    render_stub._empty_event_lists(msg)  # must not raise
    assert msg == {"workers": []}


def test_stub_message_inplace_sets_stub_and_empties_events():
    msg = {
        "events": [_text_event("a", "x"), _text_event("b", "y")],
        "workers": [{"delegation_id": "d", "events": [_text_event("w", "z")]}],
    }
    returned = render_stub.stub_message_inplace(msg, tail=1)
    assert returned is msg
    # primary (a,b) interleaved with worker (w) -> 3 renderable
    assert msg["stub"]["event_count"] == 3
    assert [e["data"]["uuid"] for e in msg["stub"]["last_events"]] == ["w"]
    assert msg["events"] == []
    assert msg["workers"][0]["events"] == []


# --- primary_events / _worker_events / _renderable edge cases -------------

def test_primary_events_missing_returns_empty_list():
    assert render_stub.primary_events({}) == []


def test_renderable_drops_non_render_types_and_metadata():
    evs = [
        _text_event("a", "x"),
        {"type": "complete"},                                  # _NON_RENDER_TYPES
        {"type": "agent_message", "data": {"type": "ai-title"}},  # metadata
    ]
    out = render_stub._renderable(evs)
    assert [e["data"]["uuid"] for e in out] == ["a"]


def test_renderable_ignores_non_dict_entries():
    out = render_stub._renderable([_text_event("a", "x"), 7, None])
    assert [e["data"]["uuid"] for e in out] == ["a"]


def test_worker_events_missing_returns_empty_list():
    assert render_stub._worker_events({}) == []
