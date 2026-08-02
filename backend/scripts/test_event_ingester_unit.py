"""Unit tests for the pure helper cluster of event_ingester.py.

Covers the summary-projection + dedup helpers that the existing
integration-style ingester tests only reach indirectly through full
ingest scans:

  - UUID extraction (`_extract_uuid`)
  - bcfile-link neutralization for dedup hashing (`_dedup_data_for_hash`)
  - render/root projection predicates (`_affects_render_projection`,
    `_affects_root_events_projection`, `_affects_root_events_candidate`)
  - frontend shape transform (`_root_event_frontend_shape`)
  - public summary projection + filtering (`_public_message_summary`,
    `_public_message_summaries`, `_summary_matches_filter`)
  - summary render/preview delegates (`_summary_render_event`,
    `_summary_preview_events`)
  - write-time owner dedup (`_is_duplicate_event_owner`)
  - the shared summary fold (`_update_summary_line`)
  - orphan-resolution bound folding (`_fold_resolutions`)

EventIngester() is cheap to construct (dict/threading init only; the
fsync thread is lazy), so instance helpers are exercised on a real
instance with directly-seeded index state. No journal files, no I/O.

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python -m pytest scripts/test_event_ingester_unit.py
"""

from __future__ import annotations

import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import event_ingester  # noqa: E402
from event_ingester import EventIngester  # noqa: E402


# --- fixtures -------------------------------------------------------------

def _agent_entry(uuid: str, seq: int, msg_id: str = "m1", sid: str = "s1",
                 **data_extra) -> dict:
    data = {"type": "assistant", "uuid": uuid}
    data.update(data_extra)
    return {"type": "agent_message", "seq": seq, "msg_id": msg_id, "sid": sid,
            "data": data}


def _new_ingester() -> EventIngester:
    return EventIngester()


# --- _extract_uuid --------------------------------------------------------

def test_extract_uuid_direct_data_field():
    assert EventIngester._extract_uuid({"uuid": "u1"}) == "u1"


def test_extract_uuid_nested_data():
    assert EventIngester._extract_uuid({"data": {"uuid": "u2"}}) == "u2"


def test_extract_uuid_ws_wrapper():
    assert EventIngester._extract_uuid(
        {"event": {"data": {"uuid": "u3"}}}) == "u3"


def test_extract_uuid_non_dict_returns_none():
    assert EventIngester._extract_uuid("nope") is None
    assert EventIngester._extract_uuid(None) is None


def test_extract_uuid_empty_returns_none():
    assert EventIngester._extract_uuid({}) is None
    assert EventIngester._extract_uuid({"data": {}}) is None
    assert EventIngester._extract_uuid({"event": {}}) is None
    assert EventIngester._extract_uuid({"event": {"data": {}}}) is None


def test_extract_uuid_direct_wins_over_nested():
    assert EventIngester._extract_uuid(
        {"uuid": "outer", "data": {"uuid": "inner"}}) == "outer"


# --- _dedup_data_for_hash -------------------------------------------------

def test_dedup_data_for_hash_neutralizes_bcfile_links():
    src = "see [file.py](bcfile:%2Fpath%2Ffile.py) here"
    out = EventIngester._dedup_data_for_hash({"text": src})
    # the whole markdown link collapses to its bare label -> two links to
    # the same label hash identically regardless of path
    assert out == {"text": "see file.py here"}


def test_dedup_data_for_hash_recurses_list_and_dict():
    payload = {
        "a": ["[x](bcfile:%2Fa)"],
        "nested": {"b": "[y](bcfile:%2Fb)"},
        "num": 7,
    }
    out = EventIngester._dedup_data_for_hash(payload)
    assert out == {"a": ["x"], "nested": {"b": "y"}, "num": 7}


def test_dedup_data_for_hash_no_link_unchanged_value():
    assert EventIngester._dedup_data_for_hash({"text": "plain"}) == {"text": "plain"}


def test_dedup_data_for_hash_does_not_mutate_input():
    payload = {"text": "[z](bcfile:%2Fz)"}
    original = copy.deepcopy(payload)
    EventIngester._dedup_data_for_hash(payload)
    assert payload == original


# --- projection predicates ------------------------------------------------

def test_affects_render_projection_types():
    for t in ("agent_message", "manager_event", "steer_prompt", "worker_event",
              "event_ownership_resolved"):
        assert EventIngester._affects_render_projection({"type": t}) is True
    assert EventIngester._affects_render_projection({"type": "complete"}) is False
    assert EventIngester._affects_render_projection({"type": "lifecycle_notice"}) is False


def test_affects_render_projection_drops_metadata():
    meta = {"type": "agent_message", "data": {"type": "ai-title"}}
    assert EventIngester._affects_render_projection(meta) is False


def test_affects_root_events_projection():
    assert EventIngester._affects_root_events_projection(
        {"type": "event_ownership_resolved"}) is True
    assert EventIngester._affects_root_events_projection({"type": "agent_message"}) is True
    assert EventIngester._affects_root_events_projection(
        {"type": "manager_event", "data": {"event": {"type": "agent_message"}}}) is True
    # worker_event / steer_prompt are NOT root-events projection types
    assert EventIngester._affects_root_events_projection({"type": "worker_event"}) is False
    assert EventIngester._affects_root_events_projection({"type": "steer_prompt"}) is False
    assert EventIngester._affects_root_events_projection(
        {"type": "agent_message", "data": {"type": "ai-title"}}) is False


def test_affects_root_events_candidate():
    # agent_message with no msg_id, non-metadata -> candidate
    assert EventIngester._affects_root_events_candidate({"type": "agent_message"}) is True
    # stamped with a msg_id -> not a candidate (owned by a message)
    assert EventIngester._affects_root_events_candidate(
        {"type": "agent_message", "msg_id": "m1"}) is False
    # manager_event with msg_id is excluded by the type guard too
    assert EventIngester._affects_root_events_candidate(
        {"type": "manager_event", "msg_id": "m1"}) is False
    # worker_event never a candidate
    assert EventIngester._affects_root_events_candidate({"type": "worker_event"}) is False
    # metadata never a candidate
    assert EventIngester._affects_root_events_candidate(
        {"type": "agent_message", "data": {"type": "ai-title"}}) is False


# --- _root_event_frontend_shape -------------------------------------------

def test_root_event_frontend_shape_manager_event_unwraps_inner():
    entry = {"type": "manager_event", "data": {"event": {"type": "agent_message", "x": 1}}}
    out = EventIngester._root_event_frontend_shape(entry)
    assert out == {"type": "agent_message", "x": 1}
    # deepcopy: mutating the output must not touch the input
    out["x"] = 2
    assert entry["data"]["event"]["x"] == 1


def test_root_event_frontend_shape_agent_message_wraps():
    entry = {"type": "agent_message", "data": {"uuid": "u", "type": "assistant"}}
    out = EventIngester._root_event_frontend_shape(entry)
    assert out == {"type": "agent_message", "data": {"uuid": "u", "type": "assistant"}}


def test_root_event_frontend_shape_manager_event_without_inner_wraps_data():
    entry = {"type": "manager_event", "data": {"uuid": "u"}}
    out = EventIngester._root_event_frontend_shape(entry)
    # falls through to the agent_message-style wrap
    assert out == {"type": "agent_message", "data": {"uuid": "u"}}


# --- public summary projection + filtering --------------------------------

def test_public_message_summary_strips_private_keys():
    summary = {"sid": "s1", "event_count": 3, "_render_uuid_idx": {"u": 0}, "_secret": 1}
    assert EventIngester._public_message_summary(summary) == {
        "sid": "s1", "event_count": 3,
    }


def test_public_message_summaries_maps_all():
    summaries = {
        "m1": {"sid": "s1", "_priv": 1},
        "m2": {"sid": "s2", "event_count": 5},
    }
    out = EventIngester._public_message_summaries(summaries)
    assert out == {"m1": {"sid": "s1"}, "m2": {"sid": "s2", "event_count": 5}}


def test_summary_matches_filter_no_filters_keeps_all():
    assert EventIngester._summary_matches_filter(
        "m1", {"sid": "s1"}, sid_filter=None, msg_ids=None) is True


def test_summary_matches_filter_sid_mismatch_drops():
    assert EventIngester._summary_matches_filter(
        "m1", {"sid": "s1"}, sid_filter="s2", msg_ids=None) is False


def test_summary_matches_filter_msg_ids_membership():
    assert EventIngester._summary_matches_filter(
        "m1", {"sid": "s1"}, sid_filter=None, msg_ids={"m1", "m9"}) is True
    assert EventIngester._summary_matches_filter(
        "m2", {"sid": "s1"}, sid_filter=None, msg_ids={"m1", "m9"}) is False


def test_summary_matches_filter_both_filters_combined():
    assert EventIngester._summary_matches_filter(
        "m1", {"sid": "s1"}, sid_filter="s1", msg_ids={"m1"}) is True
    # sid ok but msg_id not in set -> False
    assert EventIngester._summary_matches_filter(
        "m9", {"sid": "s1"}, sid_filter="s1", msg_ids={"m1"}) is False


# --- _summary_render_event / _summary_preview_events ----------------------

def test_summary_render_event_agent_message_includes_seq():
    out = EventIngester._summary_render_event(
        {"type": "agent_message", "seq": 7, "data": {"uuid": "u", "type": "assistant"}})
    assert out == {"type": "agent_message", "seq": 7, "data": {"uuid": "u", "type": "assistant"}}


def test_summary_render_event_non_render_type_returns_none():
    assert EventIngester._summary_render_event({"type": "complete"}) is None


def test_summary_render_event_manager_event_unwraps():
    out = EventIngester._summary_render_event(
        {"type": "manager_event", "seq": 2,
         "data": {"event": {"type": "agent_message", "data": {"uuid": "u"}}}})
    assert out == {"type": "agent_message", "seq": 2, "data": {"uuid": "u"}}


def test_summary_preview_events_delegates_tail_cut():
    events = [{"i": i} for i in range(5)]
    out = EventIngester._summary_preview_events(events, tail=2)
    assert out == events[-2:]


# --- _is_duplicate_event_owner --------------------------------------------

def test_is_duplicate_event_owner_first_sight_records_and_returns_false():
    ing = _new_ingester()
    assert ing._is_duplicate_event_owner("r", "h1", "m1") is False
    # second identical sighting is a duplicate
    assert ing._is_duplicate_event_owner("r", "h1", "m1") is True


def test_is_duplicate_event_owner_none_msg_id_with_existing_owners_is_dup():
    ing = _new_ingester()
    ing._is_duplicate_event_owner("r", "h1", "m1")
    # an anonymous (None) event for a hash that already has an owner -> dup
    assert ing._is_duplicate_event_owner("r", "h1", None) is True


def test_is_duplicate_event_owner_none_msg_id_first_is_not_dup():
    ing = _new_ingester()
    assert ing._is_duplicate_event_owner("r", "h1", None) is False


def test_is_duplicate_event_owner_distinct_hashes_independent():
    ing = _new_ingester()
    assert ing._is_duplicate_event_owner("r", "h1", "m1") is False
    assert ing._is_duplicate_event_owner("r", "h2", "m1") is False
    assert ing._is_duplicate_event_owner("r", "h2", "m1") is True


# --- _update_summary_line -------------------------------------------------

def test_update_summary_line_worker_event_records_byte_range():
    ing = _new_ingester()
    out: dict = {}
    res: dict = {}
    entry = {"type": "worker_event", "seq": 1, "data": {"delegation_id": "d1"}}
    ing._update_summary_line(out, res, "r", entry, 100, 200, tail=25)
    assert out == {}                                   # no msg rec for worker_event
    assert ing._worker_rows["r"]["d1"] == [(100, 200)]
    assert res == {}


def test_update_summary_line_worker_event_ignores_missing_delegation_id():
    ing = _new_ingester()
    ing._update_summary_line({}, {}, "r", {"type": "worker_event", "seq": 1, "data": {}},
                             0, 10, tail=25)
    assert ing._worker_rows.get("r") in (None, {})


def test_update_summary_line_ownership_resolution_records_target():
    ing = _new_ingester()
    res: dict = {}
    ing._update_summary_line(
        {}, res, "r",
        {"type": "event_ownership_resolved", "seq": 4, "msg_id": "ignored",
         "data": {"event_seq": 9, "message_id": "m-owner"}},
        0, 10, tail=25)
    assert res == {9: "m-owner"}


def test_update_summary_line_ownership_resolution_invalid_is_noop():
    ing = _new_ingester()
    for bad in (
        {"type": "event_ownership_resolved", "seq": 1, "data": {"event_seq": "x", "message_id": "m"}},
        {"type": "event_ownership_resolved", "seq": 1, "data": {"event_seq": 0, "message_id": "m"}},
        {"type": "event_ownership_resolved", "seq": 1, "data": {"event_seq": 5, "message_id": ""}},
    ):
        res: dict = {}
        ing._update_summary_line({}, res, "r", bad, 0, 10, tail=25)
        assert res == {}


def test_update_summary_line_entry_without_msg_id_is_noop():
    ing = _new_ingester()
    out: dict = {}
    ing._update_summary_line(out, {}, "r",
                             {"type": "agent_message", "seq": 1, "data": {"uuid": "u"}},
                             0, 10, tail=25)
    assert out == {}


def test_update_summary_line_creates_record_and_appends_event():
    ing = _new_ingester()
    out: dict = {}
    entry = _agent_entry("u1", seq=3, msg_id="m1")
    ing._update_summary_line(out, {}, "r", entry, 10, 40, tail=25)
    rec = out["m1"]
    assert rec["sid"] == "s1"
    assert rec["seq_start"] == 3 and rec["seq_end"] == 3
    assert rec["byte_start"] == 10 and rec["byte_end"] == 40
    assert rec["event_count"] == 1
    assert len(rec["last_events"]) == 1
    assert rec["_render_uuid_idx"] == {"u1": 0}


def test_update_summary_line_extends_seq_and_byte_span():
    ing = _new_ingester()
    out: dict = {}
    ing._update_summary_line(out, {}, "r", _agent_entry("u1", seq=2, msg_id="m1"), 10, 30, tail=25)
    ing._update_summary_line(out, {}, "r", _agent_entry("u2", seq=5, msg_id="m1"), 30, 70, tail=25)
    rec = out["m1"]
    assert rec["seq_start"] == 2 and rec["seq_end"] == 5
    assert rec["byte_start"] == 10 and rec["byte_end"] == 70
    assert rec["event_count"] == 2


def test_update_summary_line_worker_panel_count():
    ing = _new_ingester()
    out: dict = {}
    ing._update_summary_line(out, {}, "r", _agent_entry("u1", seq=1, msg_id="m1"), 0, 10, tail=25)
    ing._update_summary_line(out, {}, "r",
                             {"type": "worker_start", "seq": 2, "msg_id": "m1"}, 10, 20, tail=25)
    ing._update_summary_line(out, {}, "r",
                             {"type": "worker_complete", "seq": 3, "msg_id": "m1"}, 20, 30, tail=25)
    assert out["m1"]["worker_panel_event_count"] == 2
    # worker_start/complete are not render events -> last_events unchanged
    assert out["m1"]["event_count"] == 1


def test_update_summary_line_replaces_same_uuid_mutated_event():
    ing = _new_ingester()
    out: dict = {}
    ing._update_summary_line(out, {}, "r", _agent_entry("u1", seq=1, msg_id="m1", text="v0"),
                             0, 10, tail=25)
    # same uuid, different data -> in-place replace, event_count unchanged
    ing._update_summary_line(out, {}, "r", _agent_entry("u1", seq=2, msg_id="m1", text="v1"),
                             10, 20, tail=25)
    rec = out["m1"]
    assert rec["event_count"] == 1
    assert len(rec["last_events"]) == 1
    # the replaced event carries the new seq/text
    replaced = rec["last_events"][0]
    assert replaced["seq"] == 2


def test_update_summary_line_tail_trims_preview_but_keeps_count():
    ing = _new_ingester()
    out: dict = {}
    for i in range(1, 4):
        ing._update_summary_line(out, {}, "r", _agent_entry(f"u{i}", seq=i, msg_id="m1"),
                                 i * 10, i * 10 + 5, tail=2)
    rec = out["m1"]
    assert rec["event_count"] == 3            # all three counted
    assert len(rec["last_events"]) == 2       # preview trimmed to tail


def test_update_summary_line_non_render_event_creates_rec_but_no_event():
    ing = _new_ingester()
    out: dict = {}
    # 'complete' is not a render type -> _summary_render_event returns None
    ing._update_summary_line(out, {}, "r",
                             {"type": "complete", "seq": 1, "msg_id": "m1"}, 0, 10, tail=25)
    assert "m1" in out                         # rec created (seq/byte tracked)
    assert out["m1"]["event_count"] == 0
    assert out["m1"]["last_events"] == []


def test_update_summary_line_non_int_seq_then_int_adopts_seq_start():
    ing = _new_ingester()
    out: dict = {}
    # first entry carries no seq -> rec created with seq_start=None (non-int)
    ing._update_summary_line(out, {}, "r",
                             {"type": "agent_message", "msg_id": "m1",
                              "data": {"type": "assistant", "uuid": "u1"}}, 0, 10, tail=25)
    assert out["m1"]["seq_start"] is None
    # second entry with an int seq -> seq_start adopted, seq_end set
    ing._update_summary_line(out, {}, "r", _agent_entry("u2", seq=8, msg_id="m1"),
                             10, 20, tail=25)
    assert out["m1"]["seq_start"] == 8
    assert out["m1"]["seq_end"] == 8


def test_update_summary_line_skips_render_event_without_uuid():
    ing = _new_ingester()
    out: dict = {}
    # agent_message is a render type but carries no uuid -> event dropped
    ing._update_summary_line(out, {}, "r",
                             {"type": "agent_message", "seq": 1, "msg_id": "m1",
                              "data": {"type": "assistant"}}, 0, 10, tail=25)
    assert out["m1"]["event_count"] == 0
    assert out["m1"]["last_events"] == []


def test_update_summary_line_resets_corrupted_uuid_idx():
    ing = _new_ingester()
    # pre-seed a rec whose _render_uuid_idx is not a dict (defensive reset)
    out = {"m1": {
        "root_id": "r", "sid": "s1", "msg_id": "m1", "seq_start": 1, "seq_end": 1,
        "byte_start": 0, "byte_end": 10, "event_count": 0, "worker_panel_event_count": 0,
        "last_events": [], "_render_uuid_idx": "corrupted",
    }}
    ing._update_summary_line(out, {}, "r", _agent_entry("u1", seq=2, msg_id="m1"),
                             10, 20, tail=25)
    rec = out["m1"]
    assert rec["_render_uuid_idx"] == {"u1": 0}   # reset to a dict
    assert rec["event_count"] == 1


# --- _fold_resolutions ----------------------------------------------------

def test_fold_resolutions_creates_new_record_from_resolution():
    ing = _new_ingester()
    ing._seq_offsets["r"] = [0, 10, 20]
    ing._next_offset["r"] = 30
    summaries: dict = {}
    ing._fold_resolutions("r", summaries, {3: "m-new"})
    rec = summaries["m-new"]
    assert rec["seq_start"] == 3 and rec["seq_end"] == 3
    assert rec["byte_start"] == 20 and rec["byte_end"] == 30
    assert rec["event_count"] == 0 and rec["last_events"] == []


def test_fold_resolutions_folds_existing_record_bounds():
    ing = _new_ingester()
    ing._seq_offsets["r"] = [0, 10, 20, 30]
    ing._next_offset["r"] = 40
    summaries = {"m1": {
        "root_id": "r", "sid": "s1", "msg_id": "m1",
        "seq_start": 5, "seq_end": 5, "byte_start": 100, "byte_end": 110,
        "event_count": 1, "last_events": [],
    }}
    ing._fold_resolutions("r", summaries, {2: "m1"})  # seq 2 -> bytes (10, 20)
    rec = summaries["m1"]
    assert rec["seq_start"] == 2           # min(5, 2)
    assert rec["seq_end"] == 5             # max stays
    assert rec["byte_start"] == 10         # min(100, 10)
    assert rec["byte_end"] == 110          # max stays


def test_fold_resolutions_replaces_non_int_seq_start():
    ing = _new_ingester()
    ing._seq_offsets["r"] = [0, 10]
    ing._next_offset["r"] = 20
    summaries = {"m1": {
        "root_id": "r", "sid": None, "msg_id": "m1",
        "seq_start": None, "seq_end": None, "byte_start": 10, "byte_end": 20,
        "event_count": 0, "last_events": [],
    }}
    ing._fold_resolutions("r", summaries, {1: "m1"})  # seq 1 -> (0, 10)
    rec = summaries["m1"]
    assert rec["seq_start"] == 1 and rec["seq_end"] == 1


def test_fold_resolutions_skips_unknown_seq():
    ing = _new_ingester()
    # no index seeded -> _seq_byte_range returns None -> skipped
    summaries: dict = {}
    ing._fold_resolutions("r", summaries, {99: "m-x"})
    assert summaries == {}


# --- module-level import sanity ------------------------------------------

def test_module_exposes_singleton():
    assert isinstance(event_ingester.event_ingester, EventIngester)
