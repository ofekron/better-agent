"""Dedicated pytest unit-coverage owner for stores/schedule_store.py.

schedule_store.py is pure durable-store logic: CRUD, once-vs-recurring
mark_fired semantics, the due-window selector, validation bounds, the
schema-version loud-empty behavior, and the mtime/size-fingerprint read
cache. test_schedule_store.py is a standalone script-style smoke (run via
`python scripts/...`, collects 0 under pytest), so under pytest this module
had no real owner. This file covers every branch hermetically — isolated
BETTER_AGENT_HOME, no provider CLI, no real model turns.
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
_test_home.isolate("bc-test-sched-store-unit-")

from stores import schedule_store  # noqa: E402

MAX_HORIZON_SECONDS = int(schedule_store.MAX_HORIZON.total_seconds())


@pytest.fixture(autouse=True)
def _reset_store():
    """Each test starts from an empty store + cleared read cache."""
    schedule_store._data_cache = None
    path = schedule_store._path()
    if path.exists():
        path.unlink()
    yield
    schedule_store._data_cache = None


def _soon(**kw) -> str:
    kw.setdefault("hours", 1)
    return (datetime.now() + timedelta(**kw)).isoformat()


def _write_raw(payload) -> None:
    """Write arbitrary JSON straight to schedules.json, bypassing _write so
    the fingerprint cache is invalidated explicitly."""
    schedule_store._path().write_text(json.dumps(payload), encoding="utf-8")
    schedule_store._data_cache = None


# --------------------------------------------------------------------------- #
# _fingerprint / _read internals
# --------------------------------------------------------------------------- #
def test_fingerprint_missing_file_is_zero():
    assert schedule_store._fingerprint() == (0, 0)


def test_fingerprint_present_returns_stat():
    schedule_store.create(
        app_session_id="s", prompt="p", kind="once", fire_at=_soon())
    assert schedule_store._fingerprint() != (0, 0)


def test_empty_store_reads_as_empty_list():
    assert schedule_store.list_all() == []


def test_read_cache_hit_returns_isolated_deepcopy():
    schedule_store.create(
        app_session_id="s1", prompt="p", kind="once", fire_at=_soon())
    first = schedule_store.list_for_session("s1")
    second = schedule_store.list_for_session("s1")
    assert first == second
    # Mutating a returned record must not poison the parsed cache.
    first[0]["prompt"] = "MUT"
    assert schedule_store.list_for_session("s1")[0]["prompt"] == "p"


def test_read_corrupt_json_returns_empty(caplog):
    schedule_store._path().write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert schedule_store.list_all() == []
    assert any("failed to read" in r.message for r in caplog.records)


def test_read_wrong_version_returns_empty(caplog):
    _write_raw({"version": 999, "schedules": []})
    with caplog.at_level("ERROR"):
        assert schedule_store.list_all() == []
    assert any("unexpected shape/version" in r.message for r in caplog.records)


def test_read_non_dict_payload_returns_empty(caplog):
    _write_raw(["not", "a", "dict"])
    with caplog.at_level("ERROR"):
        assert schedule_store.list_all() == []
    assert any("unexpected shape/version" in r.message for r in caplog.records)


def test_read_missing_schedules_key_defaults_empty():
    _write_raw({"version": schedule_store.SCHEMA_VERSION})
    assert schedule_store.list_all() == []


# --------------------------------------------------------------------------- #
# _parse_iso
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [123, 4.5, None, ["x"]])
def test_parse_iso_rejects_non_string(value):
    with pytest.raises(ValueError, match="ISO-8601 string"):
        schedule_store._parse_iso(value)


def test_parse_iso_rejects_garbage_string():
    with pytest.raises(ValueError, match="ISO-8601 datetime"):
        schedule_store._parse_iso("not-a-date")


def test_parse_iso_rejects_tz_aware():
    iso = (datetime.now() + timedelta(hours=1)).isoformat() + "+00:00"
    with pytest.raises(ValueError, match="naive local datetime"):
        schedule_store._parse_iso(iso)


def test_parse_iso_accepts_valid_naive():
    dt = schedule_store._parse_iso(_soon())
    assert dt.tzinfo is None


# --------------------------------------------------------------------------- #
# create — validation bounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("overrides,phrase", [
    (dict(app_session_id=""), "app_session_id required"),
    (dict(app_session_id=None), "app_session_id required"),
    (dict(app_session_id=123), "app_session_id required"),
    (dict(source_task_id=123), "source_task_id must be a string"),
    (dict(prompt=""), "prompt required"),
    (dict(prompt="   "), "prompt required"),
    (dict(prompt="x" * (schedule_store.MAX_PROMPT_LEN + 1)), "exceeds"),
    (dict(kind="weird"), "kind must be"),
    (dict(kind="recurring"), "interval_seconds"),
    (dict(kind="recurring", interval_seconds=None), "interval_seconds"),
    (dict(kind="recurring", interval_seconds="60"), "interval_seconds"),
    (dict(kind="recurring", interval_seconds=True), "interval_seconds"),
    (dict(kind="recurring", interval_seconds=5), ">= 60"),
    (dict(kind="recurring", interval_seconds=MAX_HORIZON_SECONDS + 1),
     "interval exceeds max horizon"),
    (dict(fire_at=None), "fire_at required"),
    (dict(kind="once", fire_at="not-a-date"), "ISO-8601"),
    (dict(kind="once", fire_at=_soon() + "+00:00"), "naive local"),
    (dict(kind="once", fire_at=_soon(days=400)), "fire_at exceeds max horizon"),
])
def test_create_rejects_invalid(overrides, phrase):
    base = dict(app_session_id="s1", prompt="p", kind="once", fire_at=_soon())
    base.update(overrides)
    with pytest.raises(ValueError, match=phrase):
        schedule_store.create(**base)


def test_create_recurring_with_explicit_fire_at():
    rec = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=60, fire_at=_soon())
    assert rec["kind"] == "recurring"
    assert rec["interval_seconds"] == 60


def test_create_recurring_derives_first_fire_from_interval():
    before = datetime.now()
    rec = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=120)
    derived = datetime.fromisoformat(rec["fire_at"])
    assert derived >= before + timedelta(seconds=120)


def test_create_once_records_source_task_id():
    rec = schedule_store.create(
        app_session_id="s1", prompt="p", kind="once", fire_at=_soon(),
        source_task_id="task-7")
    assert rec["source_task_id"] == "task-7"


def test_create_enforces_per_session_cap():
    for i in range(schedule_store.MAX_PER_SESSION):
        schedule_store.create(
            app_session_id="cap", prompt=f"p{i}", kind="once", fire_at=_soon())
    with pytest.raises(ValueError, match="already has"):
        schedule_store.create(
            app_session_id="cap", prompt="overflow", kind="once", fire_at=_soon())


# --------------------------------------------------------------------------- #
# CRUD happy paths
# --------------------------------------------------------------------------- #
def test_crud_get_list_delete_sorted():
    a = schedule_store.create(
        app_session_id="s1", prompt="a", kind="once", fire_at=_soon(hours=2))
    b = schedule_store.create(
        app_session_id="s2", prompt="b", kind="once", fire_at=_soon(hours=1))
    assert schedule_store.get(a["id"])["prompt"] == "a"
    assert schedule_store.get("nope") is None
    # list_all is sorted by fire_at → b (1h) before a (2h).
    assert [s["id"] for s in schedule_store.list_all()] == [b["id"], a["id"]]
    assert len(schedule_store.list_for_session("s1")) == 1
    assert schedule_store.list_for_session("nope") == []
    removed = schedule_store.delete(a["id"])
    assert removed["id"] == a["id"]
    assert schedule_store.get(a["id"]) is None
    assert schedule_store.delete("nope") is None


# --------------------------------------------------------------------------- #
# due window
# --------------------------------------------------------------------------- #
def test_due_window_skips_future_and_malformed(caplog):
    past = schedule_store.create(
        app_session_id="s1", prompt="past", kind="once",
        fire_at=_soon(hours=-1))
    schedule_store.create(
        app_session_id="s1", prompt="future", kind="once", fire_at=_soon(hours=1))
    now = datetime.now()
    due = schedule_store.due(now)
    assert [d["id"] for d in due] == [past["id"]]
    # Nothing was due an hour ago either.
    assert schedule_store.due(now - timedelta(hours=2)) == []
    # A malformed record is skipped + logged, never crashes the selector.
    _write_raw({"version": schedule_store.SCHEMA_VERSION, "schedules": [
        {"id": "bad", "app_session_id": "s", "prompt": "p",
         "kind": "once", "fire_at": "not-a-date"},
    ]})
    with caplog.at_level("ERROR"):
        assert schedule_store.due(now) == []
    assert any("malformed record" in r.message for r in caplog.records)


def test_due_defaults_now():
    schedule_store.create(
        app_session_id="s1", prompt="past", kind="once",
        fire_at=_soon(hours=-1))
    assert len(schedule_store.due()) == 1


# --------------------------------------------------------------------------- #
# mark_fired
# --------------------------------------------------------------------------- #
def test_mark_fired_once_deletes():
    r = schedule_store.create(
        app_session_id="s1", prompt="once", kind="once", fire_at=_soon(hours=-1))
    schedule_store.mark_fired(r["id"])  # default now
    assert schedule_store.get(r["id"]) is None


def test_mark_fired_recurring_advances_and_stamps():
    r = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=60, fire_at=_soon(hours=-2))
    later = datetime.now() + timedelta(hours=1)
    schedule_store.mark_fired(r["id"], later)
    rec = schedule_store.get(r["id"])
    assert rec is not None
    assert datetime.fromisoformat(rec["fire_at"]) > later
    assert rec["last_fired_at"] is not None


_MISSING = object()  # sentinel: a recurring record carrying no interval key


@pytest.mark.parametrize("rid,interval", [
    ("zero", 0),        # int parse ok, but non-positive → drop
    ("typed", "x"),     # int("x") → ValueError
    ("missing", _MISSING),  # KeyError on the missing interval key
    ("nulled", None),   # int(None) → TypeError
])
def test_mark_fired_drops_malformed_recurring(rid, interval):
    record = {
        "id": rid, "app_session_id": "s", "prompt": "p",
        "kind": "recurring", "fire_at": _soon(),
    }
    if interval is not _MISSING:
        record["interval_seconds"] = interval
    _write_raw({"version": schedule_store.SCHEMA_VERSION, "schedules": [record]})
    schedule_store.mark_fired(rid, datetime.now())
    assert schedule_store.get(rid) is None


def test_mark_fired_unknown_id_skips_existing():
    schedule_store.create(
        app_session_id="s1", prompt="keep", kind="once", fire_at=_soon())
    schedule_store.mark_fired("does-not-exist")
    assert len(schedule_store.list_all()) == 1
