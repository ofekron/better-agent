"""Dedicated pytest unit-coverage owner for stores/task_trigger_store.py.

task_trigger_store.py is the durable "when" registry for Tasks: the four
trigger kinds (schedule_once/recurring, script, turn_end), the due-window
selector, turn_end event matching + idempotent receipt enqueue, the
event-launch snapshot state machine (missing/stale/stopped/current),
claim/retry/mark_fired lifecycle, and the mtime/size-fingerprint read cache
with schema-version loud-empty. It had no dedicated pytest owner (only
incidental coverage from scheduler/task integration tests), so most of its
branches were unexercised under pytest.

This file covers every branch hermetically: isolated BETTER_AGENT_HOME, real
on-disk persistence (no store mocks), no provider CLI, no real model turns.
The only stubs are `task_store.get` / `task_store.claim_event_run` — the
cross-store boundary for the event-launch path, which task_store's own owner
covers separately.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_test_home.isolate("bc-test-task-trigger-store-unit-")

from stores import task_trigger_store as tts  # noqa: E402
from stores import task_store  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_store():
    """Each test starts from an empty store + cleared read cache."""
    tts._data_cache = None
    path = tts._path()
    if path.exists():
        path.unlink()
    yield
    tts._data_cache = None


def _soon(**kw) -> str:
    kw.setdefault("hours", 1)
    return (datetime.now() + timedelta(**kw)).isoformat()


def _write_raw(payload) -> None:
    """Write arbitrary JSON straight to task_triggers.json, bypassing _write
    so the fingerprint cache is invalidated explicitly."""
    tts._path().write_text(json.dumps(payload), encoding="utf-8")
    tts._data_cache = None


def _task(task_id="task-1", **over) -> dict:
    base = dict(id=task_id, cwd="/repo", node_id="primary", stopped=False)
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# _fingerprint / _read internals
# --------------------------------------------------------------------------- #
def test_fingerprint_missing_file_is_zero():
    assert tts._fingerprint() == (0, 0)


def test_fingerprint_present_returns_stat():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    assert tts._fingerprint() != (0, 0)


def test_empty_store_reads_as_empty():
    assert tts.list_for_task("anything") == []


def test_read_cache_hit_returns_isolated_deepcopy():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    first = tts.list_for_task("task-1")
    second = tts.list_for_task("task-1")
    assert first == second
    # Mutating a returned record must not poison the parsed cache.
    first[0]["task_id"] = "MUT"
    assert tts.list_for_task("task-1")[0]["task_id"] == "task-1"


def test_read_corrupt_json_returns_empty(caplog):
    tts._path().write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert tts.list_for_task("task-1") == []
    assert any("failed to read" in r.message for r in caplog.records)


def test_read_wrong_version_returns_empty(caplog):
    _write_raw({"version": 999, "triggers": []})
    with caplog.at_level("ERROR"):
        assert tts.list_for_task("task-1") == []
    assert any("unexpected shape/version" in r.message for r in caplog.records)


def test_read_non_dict_payload_returns_empty(caplog):
    _write_raw(["not", "a", "dict"])
    with caplog.at_level("ERROR"):
        assert tts.list_for_task("task-1") == []
    msg = [r.message for r in caplog.records if "unexpected" in r.message]
    assert msg and "list" in msg[0]  # type-name branch


def test_read_missing_triggers_key_defaults_empty():
    _write_raw({"version": tts.SCHEMA_VERSION})
    assert tts.list_for_task("task-1") == []


def test_read_triggers_not_list_returns_empty():
    _write_raw({"version": tts.SCHEMA_VERSION, "triggers": {"nope": 1}})
    assert tts.list_for_task("task-1") == []


# --------------------------------------------------------------------------- #
# _parse_iso
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [123, 4.5, None, ["x"]])
def test_parse_iso_rejects_non_string(value):
    with pytest.raises(ValueError, match="ISO-8601 datetime"):
        tts._parse_iso(value)


def test_parse_iso_rejects_garbage_string():
    with pytest.raises(ValueError, match="ISO-8601 datetime"):
        tts._parse_iso("not-a-date")


def test_parse_iso_rejects_tz_aware():
    iso = (datetime.now() + timedelta(hours=1)).isoformat() + "+00:00"
    with pytest.raises(ValueError, match="naive local datetime"):
        tts._parse_iso(iso)


def test_parse_iso_accepts_valid_naive():
    assert tts._parse_iso(_soon()).tzinfo is None


# --------------------------------------------------------------------------- #
# register_for_task — kinds + validation
# --------------------------------------------------------------------------- #
def test_register_requires_task_id():
    assert tts.register_for_task(_task(id="", trigger={"kind": "manual"})) == []


def test_register_stopped_task_arms_nothing():
    # A stopped task still unregisters any prior records, then arms none.
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    assert len(tts.list_for_task("task-1")) == 1
    out = tts.register_for_task(_task(stopped=True))
    assert out == []
    assert tts.list_for_task("task-1") == []


def test_register_manual_arms_nothing():
    assert tts.register_for_task(_task(trigger={"kind": "manual"})) == []
    assert tts.list_for_task("task-1") == []


def test_register_defaults_trigger_to_manual():
    assert tts.register_for_task(_task(trigger=None)) == []
    assert tts.list_for_task("task-1") == []


def test_register_schedule_once():
    recs = tts.register_for_task(
        _task(cwd="/x", node_id="n1", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "schedule_once"
    assert rec["interval_seconds"] is None
    assert rec["task_cwd"] == "/x"
    assert rec["task_node_id"] == "n1"
    assert rec["last_fired_at"] is None
    assert rec["created_at"]
    # Returned copies must not be the live record.
    recs[0]["task_id"] = "MUT"
    assert tts.list_for_task("task-1")[0]["task_id"] == "task-1"


def test_register_schedule_recurring_with_explicit_fire_at():
    recs = tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "recurring", "interval_seconds": 120, "fire_at": _soon()}}))
    assert recs[0]["kind"] == "schedule_recurring"
    assert recs[0]["interval_seconds"] == 120


def test_register_schedule_recurring_derives_first_fire():
    before = datetime.now()
    recs = tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "recurring", "interval_seconds": 300}}))
    derived = datetime.fromisoformat(recs[0]["fire_at"])
    assert derived >= before + timedelta(seconds=300)


def test_register_schedule_once_bad_fire_at_raises():
    with pytest.raises(ValueError, match="ISO-8601"):
        tts.register_for_task(
            _task(trigger={"kind": "schedule", "config": {
                "mode": "once", "fire_at": "nope"}}))


def test_register_schedule_recurring_bad_interval_raises():
    with pytest.raises(ValueError):
        tts.register_for_task(
            _task(trigger={"kind": "schedule", "config": {
                "mode": "recurring", "interval_seconds": "oops"}}))


def test_register_script_kind():
    recs = tts.register_for_task(
        _task(trigger={"kind": "script", "config": {
            "detector": ["ls"], "poll_interval_seconds": 60}}))
    assert recs[0]["kind"] == "script"
    assert recs[0]["interval_seconds"] == 60
    assert recs[0]["detector"] == ["ls"]


def test_register_script_default_poll_interval():
    recs = tts.register_for_task(
        _task(trigger={"kind": "script", "config": {"detector": ["ls"]}}))
    assert recs[0]["interval_seconds"] == 300


def test_register_turn_end_kind():
    recs = tts.register_for_task(
        _task(trigger={"kind": "turn_end", "config": {
            "outcomes": ["complete", "failed"], "reasons": ["x"],
            "provider_kind": "codex"}}))
    rec = recs[0]
    assert rec["kind"] == "turn_end"
    assert rec["outcomes"] == ["complete", "failed"]
    assert rec["reasons"] == ["x"]
    assert rec["provider_kind"] == "codex"
    assert rec["trigger_config"] == {
        "outcomes": ["complete", "failed"], "reasons": ["x"],
        "provider_kind": "codex"}


def test_register_turn_end_defaults_outcomes():
    recs = tts.register_for_task(_task(trigger={"kind": "turn_end", "config": {}}))
    assert recs[0]["outcomes"] == ["complete"]
    assert recs[0]["reasons"] is None
    assert recs[0]["provider_kind"] is None


def test_register_replaces_prior_set():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    assert len(tts.list_for_task("task-1")) == 1
    tts.register_for_task(
        _task(trigger={"kind": "script", "config": {"detector": ["ls"]}}))
    recs = tts.list_for_task("task-1")
    assert len(recs) == 1 and recs[0]["kind"] == "script"


# --------------------------------------------------------------------------- #
# unregister_task
# --------------------------------------------------------------------------- #
def test_unregister_removes_only_owned_and_skips_write_when_unchanged():
    tts.register_for_task(
        _task(id="a", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    tts.register_for_task(
        _task(id="b", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    tts.unregister_task("a")
    assert tts.list_for_task("a") == []
    assert len(tts.list_for_task("b")) == 1
    # Unregistering a task with no records is a no-op (no write path taken).
    tts.unregister_task("never-existed")
    assert len(tts.list_for_task("b")) == 1


# --------------------------------------------------------------------------- #
# due window
# --------------------------------------------------------------------------- #
def test_due_skips_future_turn_end_and_malformed(caplog):
    # Each register_for_task(task_id) wipes that task's prior triggers, so the
    # three triggers need distinct task ids to coexist in the store.
    past = tts.register_for_task(
        _task(id="task-past", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=-1)}}))[0]
    tts.register_for_task(
        _task(id="task-fut", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=1)}}))
    tts.register_for_task(
        _task(id="task-te", trigger={"kind": "turn_end",
                                     "config": {"outcomes": ["complete"]}}))
    now = datetime.now()
    due = tts.due(now)
    assert [d["id"] for d in due] == [past["id"]]
    # Nothing due two hours ago.
    assert tts.due(now - timedelta(hours=2)) == []
    # Malformed record is skipped + logged, oldest-first sort still applies.
    _write_raw({"version": tts.SCHEMA_VERSION, "triggers": [
        {"id": "bad", "kind": "schedule_once", "fire_at": "not-a-date"},
        past,
    ]})
    with caplog.at_level("ERROR"):
        assert [d["id"] for d in tts.due(now)] == [past["id"]]
    assert any("malformed record" in r.message for r in caplog.records)


def test_due_defaults_now():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=-1)}}))
    assert len(tts.due()) == 1


def test_due_sorts_oldest_first():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=-1)}}))
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=-5)}}))
    due = tts.due(datetime.now())
    assert [datetime.fromisoformat(d["fire_at"]) for d in due] == sorted(
        datetime.fromisoformat(d["fire_at"]) for d in due)


# --------------------------------------------------------------------------- #
# matching_turn_end
# --------------------------------------------------------------------------- #
def test_matching_turn_end_filters():
    tts.register_for_task(
        _task(id="task-sched", trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))  # non-turn_end: skipped in the loop
    tts.register_for_task(
        _task(id="task-te", cwd="/repo", trigger={"kind": "turn_end", "config": {
            "outcomes": ["complete"], "reasons": ["done"]}}))
    # outcome match + reason match (schedule trigger is skipped along the way)
    assert len(tts.matching_turn_end("lifecycle.turn_complete", "done")) == 1
    # outcome mismatch
    assert tts.matching_turn_end("lifecycle.turn_failed", "done") == []
    # reason mismatch
    assert tts.matching_turn_end("lifecycle.turn_complete", "other") == []


def test_matching_turn_end_no_reasons_filter_matches_any():
    tts.register_for_task(
        _task(trigger={"kind": "turn_end", "config": {"outcomes": ["complete"]}}))
    assert len(tts.matching_turn_end("lifecycle.turn_complete", "whatever")) == 1


def test_matching_turn_end_non_lifecycle_event_returns_empty():
    assert tts.matching_turn_end("some.other_event", None) == []


# --------------------------------------------------------------------------- #
# enqueue_turn_end
# --------------------------------------------------------------------------- #
def _register_turn_end(**cfg):
    base = {"outcomes": ["complete"]}
    base.update(cfg)
    return tts.register_for_task(
        _task(cwd="/repo", node_id="primary", trigger={
            "kind": "turn_end", "config": base}))[0]


def test_enqueue_non_lifecycle_event_returns_zero():
    assert tts.enqueue_turn_end(
        event_type="other.event", event_key="k", root_id="r", session_id="s",
        reason=None, timestamp=_soon(), provider_kind=None,
        cwd="/repo", node_id="primary") == 0


def test_enqueue_creates_receipt_for_matching_trigger():
    trig = _register_turn_end(provider_kind="codex")
    n = tts.enqueue_turn_end(
        event_type="lifecycle.turn_complete", event_key="evt-1", root_id="r",
        session_id="s", reason=None, timestamp=_soon(), provider_kind="codex",
        cwd="/repo", node_id="primary")
    assert n == 1
    receipts = [t for t in tts.list_for_task("task-1") if t.get("kind") == "turn_end_once"]
    assert len(receipts) == 1
    assert receipts[0]["source_trigger_id"] == trig["id"]
    assert receipts[0]["interval_seconds"] == tts.TURN_END_RETRY_SECONDS
    assert receipts[0]["context"]["session_id"] == "s"


def test_enqueue_is_idempotent_on_repeat():
    _register_turn_end()
    kwargs = dict(
        event_type="lifecycle.turn_complete", event_key="evt-1", root_id="r",
        session_id="s", reason=None, timestamp=_soon(), provider_kind=None,
        cwd="/repo", node_id="primary")
    assert tts.enqueue_turn_end(**kwargs) == 1
    # Same event_key → deterministic receipt id → dedup, no second receipt.
    assert tts.enqueue_turn_end(**kwargs) == 0
    receipts = [t for t in tts.list_for_task("task-1") if t.get("kind") == "turn_end_once"]
    assert len(receipts) == 1


def test_enqueue_skips_on_each_filter_mismatch():
    _register_turn_end(outcomes=["complete"], reasons=["done"], provider_kind="codex")
    common = dict(event_key="k", root_id="r", session_id="s", timestamp=_soon(),
                  cwd="/repo", node_id="primary")
    # wrong cwd
    assert tts.enqueue_turn_end(event_type="lifecycle.turn_complete", reason="done",
                                provider_kind="codex", **{**common, "cwd": "/elsewhere"}) == 0
    # wrong node
    assert tts.enqueue_turn_end(event_type="lifecycle.turn_complete", reason="done",
                                provider_kind="codex", **{**common, "node_id": "other"}) == 0
    # wrong outcome
    assert tts.enqueue_turn_end(event_type="lifecycle.turn_failed", reason="done",
                                provider_kind="codex", **common) == 0
    # wrong reason
    assert tts.enqueue_turn_end(event_type="lifecycle.turn_complete", reason="nope",
                                provider_kind="codex", **common) == 0
    # wrong provider
    assert tts.enqueue_turn_end(event_type="lifecycle.turn_complete", reason="done",
                                provider_kind="claude", **common) == 0
    # Only the original turn_end trigger remains — no receipts materialized.
    receipts = [t for t in tts.list_for_task("task-1")
                if t.get("kind") == "turn_end_once"]
    assert receipts == []


def test_enqueue_node_id_normalizes_none_to_primary():
    # trigger registered with default node_id "primary"; event arrives with
    # node_id None -> normalized to "primary" -> match.
    _register_turn_end()
    assert tts.enqueue_turn_end(
        event_type="lifecycle.turn_complete", event_key="k", root_id="r",
        session_id="s", reason=None, timestamp=_soon(), provider_kind=None,
        cwd="/repo", node_id=None) == 1


# --------------------------------------------------------------------------- #
# event_launch_snapshot / receipt_task_snapshot
# --------------------------------------------------------------------------- #
def _seed_receipt(extra_trigger=None) -> str:
    """Register a turn_end trigger, fire one event to materialize a
    turn_end_once receipt, return the receipt id."""
    trig = _register_turn_end()
    tts.enqueue_turn_end(
        event_type="lifecycle.turn_complete", event_key="evt-1", root_id="r",
        session_id="s", reason=None, timestamp=_soon(), provider_kind=None,
        cwd="/repo", node_id="primary")
    receipt = next(
        t for t in tts.list_for_task("task-1") if t.get("kind") == "turn_end_once")
    return receipt["id"]


def test_snapshot_missing_for_unknown_id():
    status, task, rec = tts.event_launch_snapshot("no-such-id")
    assert (status, task, rec) == ("missing", None, None)


def test_snapshot_missing_when_id_is_not_a_receipt():
    # A bare turn_end trigger (not a turn_end_once receipt) is not launchable.
    trig = _register_turn_end()
    status, task, rec = tts.event_launch_snapshot(trig["id"])
    assert (status, task, rec) == ("missing", None, None)


def test_snapshot_stale_when_source_trigger_gone():
    rid = _seed_receipt()
    # Remove the source turn_end trigger, leaving the receipt orphaned.
    _write_raw({"version": tts.SCHEMA_VERSION, "triggers": [
        t for t in tts._read()["triggers"] if t.get("kind") == "turn_end_once"]})
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stale"
    assert rec is not None and rec["id"] == rid


def test_snapshot_stale_when_trigger_config_drifted():
    rid = _seed_receipt()
    data = tts._read()
    for t in data["triggers"]:
        if t.get("kind") == "turn_end":
            t["trigger_config"] = {"changed": True}
    _write_raw(data)
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stale"


def test_snapshot_stopped_when_task_stopped(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(stopped=True))
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stopped"
    assert task is not None and task["stopped"] is True


def test_snapshot_stopped_when_task_missing(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: None)
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stopped"
    assert task is None


def test_snapshot_stale_when_task_trigger_kind_changed(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(
        trigger={"kind": "schedule", "config": {}}))
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stale"


def test_snapshot_stale_when_task_trigger_config_changed(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(
        trigger={"kind": "turn_end", "config": {"outcomes": ["failed"]}}))
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "stale"


def test_snapshot_current(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(
        trigger={"kind": "turn_end", "config": {"outcomes": ["complete"]}}))
    status, task, rec = tts.event_launch_snapshot(rid)
    assert status == "current"
    assert task is not None
    assert rec["id"] == rid


def test_receipt_task_snapshot_wraps_status(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(
        trigger={"kind": "turn_end", "config": {"outcomes": ["complete"]}}))
    ok, task = tts.receipt_task_snapshot(rid)
    assert ok is True and task is not None
    monkeypatch.setattr(task_store, "get", lambda tid: _task(stopped=True))
    ok, task = tts.receipt_task_snapshot(rid)
    assert ok is False


# --------------------------------------------------------------------------- #
# claim_event_run
# --------------------------------------------------------------------------- #
def test_claim_returns_snapshot_status_when_not_current(monkeypatch):
    rid = _seed_receipt()
    monkeypatch.setattr(task_store, "get", lambda tid: _task(stopped=True))
    status, task = tts.claim_event_run(
        rid, "sess", lifecycle_msg_id="m", expected_task_updated_at="never")
    assert status == "stopped"


def test_claim_stale_on_updated_at_mismatch(monkeypatch):
    rid = _seed_receipt()
    task = _task(updated_at="2020-01-01T00:00:00",
                 trigger={"kind": "turn_end", "config": {"outcomes": ["complete"]}})
    monkeypatch.setattr(task_store, "get", lambda tid: task)
    status, returned = tts.claim_event_run(
        rid, "sess", lifecycle_msg_id="m", expected_task_updated_at="other")
    assert status == "stale"
    assert returned is task


def test_claim_delegates_to_task_store_when_current(monkeypatch):
    rid = _seed_receipt()
    task = _task(updated_at="2020-01-01T00:00:00",
                 trigger={"kind": "turn_end", "config": {"outcomes": ["complete"]}})
    monkeypatch.setattr(task_store, "get", lambda tid: task)
    captured = {}

    def fake_claim(task_id, session_id, **kw):
        captured.update(task_id=task_id, session_id=session_id, **kw)
        return ("admitted", {"session_id": session_id})

    monkeypatch.setattr(task_store, "claim_event_run", fake_claim)
    status, returned = tts.claim_event_run(
        rid, "sess", lifecycle_msg_id="m",
        expected_task_updated_at="2020-01-01T00:00:00", now=datetime.now())
    assert status == "admitted"
    assert captured["task_id"] == task["id"]
    assert captured["session_id"] == "sess"
    assert captured["lifecycle_msg_id"] == "m"


def test_claim_unknown_receipt_returns_missing(monkeypatch):
    status, task = tts.claim_event_run(
        "no-such", "sess", lifecycle_msg_id="m",
        expected_task_updated_at="x")
    assert (status, task) == ("missing", None)


# --------------------------------------------------------------------------- #
# retry_later
# --------------------------------------------------------------------------- #
def test_retry_later_advances_turn_end_once_receipt():
    rid = _seed_receipt()
    before = next(t for t in tts._read()["triggers"] if t["id"] == rid)["fire_at"]
    now = datetime.now()
    tts.retry_later(rid, now)
    after = next(t for t in tts._read()["triggers"] if t["id"] == rid)["fire_at"]
    assert datetime.fromisoformat(after) >= now + timedelta(
        seconds=tts.TURN_END_RETRY_SECONDS - 1)
    assert after != before


def test_retry_later_unknown_id_is_noop():
    before = tts._read()["triggers"]
    tts.retry_later("missing")
    assert tts._read()["triggers"] == before


def test_retry_later_ignores_non_receipt_trigger():
    # A turn_end trigger (not a turn_end_once receipt) has no fire_at to
    # advance; retry_later returns without writing, leaving the store untouched.
    trig = _register_turn_end()
    before = tts._read()["triggers"]
    tts.retry_later(trig["id"])
    assert tts._read()["triggers"] == before


# --------------------------------------------------------------------------- #
# mark_fired
# --------------------------------------------------------------------------- #
def test_mark_fired_once_deletes():
    rec = tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon(hours=-1)}}))[0]
    tts.mark_fired(rec["id"])
    assert tts.list_for_task("task-1") == []


def test_mark_fired_recurring_advances_and_stamps():
    rec = tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "recurring", "interval_seconds": 120,
            "fire_at": _soon(hours=-2)}}))[0]
    later = datetime.now() + timedelta(hours=1)
    tts.mark_fired(rec["id"], later)
    kept = tts.list_for_task("task-1")[0]
    assert datetime.fromisoformat(kept["fire_at"]) > later
    assert kept["last_fired_at"] is not None


def test_mark_fired_turn_end_once_deletes():
    rid = _seed_receipt()
    tts.mark_fired(rid)
    assert all(t["id"] != rid for t in tts._read()["triggers"])


def test_mark_fired_advances_across_multiple_elapsed_intervals():
    # fire_at far in the past, small interval -> the while-loop must iterate
    # several times to land past `now`.
    rec = tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "recurring", "interval_seconds": 60,
            "fire_at": _soon(hours=-5)}}))[0]
    now = datetime.now()
    tts.mark_fired(rec["id"], now)
    kept = tts.list_for_task("task-1")[0]
    nxt = datetime.fromisoformat(kept["fire_at"])
    assert nxt > now
    assert (nxt - now) <= timedelta(seconds=120)


_MISSING = object()  # sentinel: a recurring record carrying no interval key


@pytest.mark.parametrize("rid,interval", [
    ("missing", _MISSING),  # KeyError on the missing interval key
    ("typed", "x"),         # int("x") -> ValueError
    ("nulled", None),       # int(None) -> TypeError
])
def test_mark_fired_drops_malformed_recurring(rid, interval, caplog):
    record = {"id": rid, "kind": "schedule_recurring", "fire_at": _soon()}
    if interval is not _MISSING:
        record["interval_seconds"] = interval
    _write_raw({"version": tts.SCHEMA_VERSION, "triggers": [record]})
    with caplog.at_level("ERROR"):
        tts.mark_fired(rid, datetime.now())
    assert all(t["id"] != rid for t in tts._read()["triggers"])
    assert any("malformed record" in r.message for r in caplog.records)


def test_mark_fired_zero_interval_advances_without_dropping(caplog):
    # int(0) parses, so this is NOT a malformed-drop: it advances normally.
    _write_raw({"version": tts.SCHEMA_VERSION, "triggers": [
        {"id": "zero", "kind": "schedule_recurring",
         "fire_at": _soon(), "interval_seconds": 0}]})
    with caplog.at_level("ERROR"):
        tts.mark_fired("zero", datetime.now())
    kept = [t for t in tts._read()["triggers"] if t["id"] == "zero"]
    assert len(kept) == 1
    assert all("malformed" not in r.message for r in caplog.records)


def test_mark_fired_unknown_id_skips_existing():
    tts.register_for_task(
        _task(trigger={"kind": "schedule", "config": {
            "mode": "once", "fire_at": _soon()}}))
    tts.mark_fired("does-not-exist")
    assert len(tts.list_for_task("task-1")) == 1
