"""Unit coverage for schedules_api.py.

Distinct from test_schedules_api.py, which is a standalone full-stack
contract script (TestClient against the real app). Here we call the async
handlers directly with faked dependencies (no live backend, no provider
turn) to cover every branch of the schedule dispatch logic.
"""
from __future__ import annotations

import asyncio
import math
import sys

import pytest
from fastapi import HTTPException

import internal_guards
import schedules_api

HEADER = "tok"


def _run(coro):
    return asyncio.run(coro)


class FakeScheduleStore:
    def __init__(self):
        self._list_all = []
        self._for_session = []
        self._get_rec = {"id": "s1", "app_session_id": "sess"}
        self.created = None
        self.deleted = []
        self._create_raises = False

    def create(self, **kwargs):
        if self._create_raises:
            raise ValueError("bad schedule")
        self.created = kwargs
        return {"id": "new-sched", **kwargs}

    def list_for_session(self, app_session_id):
        return self._for_session

    def get(self, schedule_id):
        return self._get_rec

    def delete(self, schedule_id):
        self.deleted.append(schedule_id)
        return self._delete_result

    def list_all(self):
        return self._list_all

    _delete_result = {"id": "s1", "app_session_id": "sess"}


class FakeManager:
    """Stand-in for session_manager.manager (sync list/get)."""

    def __init__(self, summaries=(), by_id=None):
        self._summaries = summaries
        self._by_id = by_id or {}

    def list(self):
        return self._summaries

    def get(self, sid):
        return self._by_id.get(sid)


@pytest.fixture
def fakes(monkeypatch):
    store = FakeScheduleStore()
    monkeypatch.setattr(schedules_api, "schedule_store", store)

    broadcasts = []

    async def fake_broadcast(coordinator, app_session_id):
        broadcasts.append(app_session_id)

    monkeypatch.setattr(schedules_api, "broadcast_schedules", fake_broadcast)

    # session_exists True by default; override via state["exists"].
    state = {"exists": True}

    async def fake_exists(session_id):
        return state["exists"]

    monkeypatch.setattr(schedules_api, "_session_exists", fake_exists)

    # No pending task owner by default.
    class FakeTaskStore:
        @staticmethod
        def find_pending_run_for_session(session_id):
            return None

    import stores
    monkeypatch.setitem(sys.modules, "stores.task_store", FakeTaskStore)
    monkeypatch.setattr(stores, "task_store", FakeTaskStore, raising=False)

    internal_guards.configure(lambda: object())  # authority valid
    schedules_api.configure(coordinator=object())

    return {
        "store": store,
        "broadcasts": broadcasts,
        "state": state,
        "task_store": FakeTaskStore,
    }


# --------------------------------------------------------------- gate / coord

def test_authority_invalid_raises_403(monkeypatch):
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    with pytest.raises(HTTPException) as exc:
        _run(schedules_api.internal_schedules({"action": "list"}, "tok"))
    assert exc.value.status_code == 403


def test_session_not_found_returns_error(fakes):
    fakes["state"]["exists"] = False
    res = _run(schedules_api.internal_schedules(
        {"action": "list", "app_session_id": "ghost"}, "tok"))
    assert res["success"] is False
    assert "error" in res


def test_coordinator_unconfigured_raises_503(monkeypatch):
    monkeypatch.setattr(schedules_api, "_coordinator_ref", None)
    with pytest.raises(HTTPException) as exc:
        schedules_api._coordinator()
    assert exc.value.status_code == 503


# ----------------------------------------------------------- create action

def test_create_with_fire_at_success_broadcasts(fakes):
    res = _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once", "fire_at": "2030-01-01T00:00:00"}, "tok"))
    assert res["success"] is True
    assert res["schedule"]["id"] == "new-sched"
    assert fakes["store"].created["fire_at"] == "2030-01-01T00:00:00"
    assert fakes["store"].created["source_task_id"] is None
    assert fakes["broadcasts"] == ["sess"]


def test_create_attaches_source_task_id(fakes, monkeypatch):
    class OwnerTaskStore:
        @staticmethod
        def find_pending_run_for_session(session_id):
            return ("task-9", {"x": 1})
    import stores
    monkeypatch.setitem(sys.modules, "stores.task_store", OwnerTaskStore)
    monkeypatch.setattr(stores, "task_store", OwnerTaskStore, raising=False)
    _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once", "fire_at": "2030-01-01T00:00:00"}, "tok"))
    assert fakes["store"].created["source_task_id"] == "task-9"


def test_create_value_error(fakes):
    fakes["store"]._create_raises = True
    res = _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once", "fire_at": "2030-01-01T00:00:00"}, "tok"))
    assert res == {"success": False, "error": "bad schedule"}


def test_create_with_valid_delay_computes_fire_at(fakes):
    res = _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once", "delay_seconds": 60}, "tok"))
    assert res["success"] is True
    fire_at = fakes["store"].created["fire_at"]
    # Computed fire_at is an ISO timestamp roughly now+60s.
    assert "T" in fire_at
    assert fire_at != "2030-01-01T00:00:00"


@pytest.mark.parametrize("bad_delay,label", [
    (-1, "negative"),
    ("60", "non-numeric string"),
    (True, "bool"),
    (math.inf, "non-finite"),
    (366 * 24 * 3600 + 1, "over one year"),
])
def test_create_delay_rejected(fakes, bad_delay, label):
    res = _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once", "delay_seconds": bad_delay}, "tok"))
    assert res["success"] is False, label
    assert "delay_seconds" in res["error"]


def test_create_no_fire_at_no_delay(fakes):
    """Neither fire_at nor delay -> create called with fire_at=None, no guard."""
    res = _run(schedules_api.internal_schedules(
        {"action": "create", "app_session_id": "sess", "prompt": "go",
         "kind": "once"}, "tok"))
    assert res["success"] is True
    assert fakes["store"].created["fire_at"] is None


# ------------------------------------------------------------- list / delete

def test_list_returns_session_schedules(fakes):
    fakes["store"]._for_session = [{"id": "a"}, {"id": "b"}]
    res = _run(schedules_api.internal_schedules(
        {"action": "list", "app_session_id": "sess"}, "tok"))
    assert res == {"success": True, "schedules": [{"id": "a"}, {"id": "b"}]}


def test_delete_success(fakes):
    res = _run(schedules_api.internal_schedules(
        {"action": "delete", "app_session_id": "sess", "schedule_id": "s1"}, "tok"))
    assert res == {"success": True}
    assert fakes["store"].deleted == ["s1"]
    assert fakes["broadcasts"] == ["sess"]


def test_delete_unknown_schedule(fakes):
    fakes["store"]._get_rec = None
    res = _run(schedules_api.internal_schedules(
        {"action": "delete", "app_session_id": "sess", "schedule_id": "s1"}, "tok"))
    assert res == {"success": False, "error": "unknown schedule_id"}


def test_delete_session_mismatch(fakes):
    fakes["store"]._get_rec = {"id": "s1", "app_session_id": "other"}
    res = _run(schedules_api.internal_schedules(
        {"action": "delete", "app_session_id": "sess", "schedule_id": "s1"}, "tok"))
    assert res == {"success": False, "error": "unknown schedule_id"}


def test_unknown_action(fakes):
    res = _run(schedules_api.internal_schedules(
        {"action": "frobnicate", "app_session_id": "sess"}, "tok"))
    assert res["success"] is False
    assert "create|list|delete" in res["error"]


# ------------------------------------------------------- user-facing endpoints

def test_get_all_schedules_enriches_and_resolves(monkeypatch):
    store = FakeScheduleStore()
    # rec_a: sid in names (root). rec_b: sid not in names, resolved via get.
    # rec_c: sid not in names, get returns None (deleted/fork orphan).
    store._list_all = [
        {"id": "a", "app_session_id": "sid_a"},
        {"id": "b", "app_session_id": "sid_b"},
        {"id": "c", "app_session_id": "sid_c"},
    ]
    monkeypatch.setattr(schedules_api, "schedule_store", store)
    summaries = [{"id": "sid_a", "name": "Alpha"}]
    manager = FakeManager(summaries=summaries,
                          by_id={"sid_b": {"id": "sid_b", "name": "Beta"}})
    monkeypatch.setattr(schedules_api, "session_manager", manager)
    schedules_api.configure(coordinator=object())

    res = _run(schedules_api.get_all_schedules())
    by_id = {s["id"]: s for s in res["schedules"]}
    assert by_id["a"]["session_name"] == "Alpha"
    assert by_id["a"]["session_exists"] is True
    assert by_id["b"]["session_name"] == "Beta"
    assert by_id["b"]["session_exists"] is True
    assert by_id["c"]["session_name"] is None
    assert by_id["c"]["session_exists"] is False


def test_delete_schedule_by_id_success(fakes, monkeypatch):
    async def fake_broadcast(coordinator, app_session_id):
        fakes["broadcasts"].append(app_session_id)
    monkeypatch.setattr(schedules_api, "broadcast_schedules", fake_broadcast)
    res = _run(schedules_api.delete_schedule_by_id("s1"))
    assert res == {"success": True}
    assert fakes["broadcasts"] == ["sess"]


def test_delete_schedule_by_id_not_found(monkeypatch):
    store = FakeScheduleStore()
    store._delete_result = None
    monkeypatch.setattr(schedules_api, "schedule_store", store)
    with pytest.raises(HTTPException) as exc:
        _run(schedules_api.delete_schedule_by_id("nope"))
    assert exc.value.status_code == 404
