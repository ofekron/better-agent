"""Dedicated owner for background_work_capability.py — the extension-facing
surface over the background-work registry: owner-identity via contextvar,
sliding-window call budget (429), BackgroundWorkRejected -> 400 funnel, and the
report/update/finish/dismiss capability handlers.

Unit tier: the registry is an in-memory collaborator, so its methods are
patched to drive every branch deterministically. The budget deque is reset
between tests for hermeticity.
"""

import asyncio
import time
from collections import deque
from contextvars import ContextVar

import pytest
from fastapi import HTTPException

import background_work
from background_work import BackgroundWorkRejected
import background_work_capability as bwc


@pytest.fixture(autouse=True)
def _clear_budget():
    with bwc._budget_lock:
        bwc._budget.clear()
    yield
    with bwc._budget_lock:
        bwc._budget.clear()


def _make_handlers(caller_var):
    """Run the registrar with a fake register() that captures the closures."""
    handlers = {}

    def register(family, name, payload_cls, fn):
        handlers[name] = (fn, payload_cls)

    bwc.register_background_work(register, caller_var)
    return handlers


def _run(coro):
    return asyncio.run(coro)


# --- _charge_budget -------------------------------------------------------

def test_charge_budget_happy_appends_under_limit(monkeypatch):
    monkeypatch.setattr(bwc, "_BUDGET_CALLS", 5)
    bwc._charge_budget("ext")
    with bwc._budget_lock:
        assert len(bwc._budget["ext"]) == 1


def test_charge_budget_evicts_stale_entries(monkeypatch):
    monkeypatch.setattr(bwc, "_BUDGET_CALLS", 5)
    stale = time.monotonic() - bwc._BUDGET_WINDOW_S - 1.0
    with bwc._budget_lock:
        bwc._budget.setdefault("ext", deque()).append(stale)
    bwc._charge_budget("ext")
    with bwc._budget_lock:
        calls = bwc._budget["ext"]
        assert len(calls) == 1
        assert calls[0] != stale


def test_charge_budget_rejects_over_limit_with_429(monkeypatch):
    monkeypatch.setattr(bwc, "_BUDGET_CALLS", 2)
    bwc._charge_budget("ext")
    bwc._charge_budget("ext")
    with pytest.raises(HTTPException) as exc:
        bwc._charge_budget("ext")
    assert exc.value.status_code == 429


# --- forget_extension -----------------------------------------------------

def test_forget_extension_removes_existing_budget():
    with bwc._budget_lock:
        bwc._budget.setdefault("ext", deque()).append(time.monotonic())
    bwc.forget_extension("ext")
    with bwc._budget_lock:
        assert "ext" not in bwc._budget


def test_forget_extension_missing_is_noop():
    bwc.forget_extension("never-existed")


# --- _item_id -------------------------------------------------------------

def test_item_id_format():
    assert bwc._item_id("ext-7", "job-9") == f"{background_work.OWNER_EXTENSION}:ext-7:job-9"


# --- capability handlers --------------------------------------------------

def test_handler_unresolved_caller_raises_403():
    caller = ContextVar("caller", default="")
    handlers = _make_handlers(caller)
    with pytest.raises(HTTPException) as exc:
        _run(handlers["report"][0](bwc.ReportPayload(local_id="x", label="y")))
    assert exc.value.status_code == 403


def test_report_returns_registry_item_id(monkeypatch):
    monkeypatch.setattr(bwc.background_work_registry, "report", lambda **kw: "item-42")
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    result = _run(handlers["report"][0](
        bwc.ReportPayload(local_id="job", label="working", detail="d", phase="p",
                          progress={"done": 1}, session_id="s", dismissible=False)))
    assert result == {"id": "item-42"}


def test_report_translates_rejection_to_400(monkeypatch):
    def _raise(*a, **k):
        raise BackgroundWorkRejected("bad label")
    monkeypatch.setattr(bwc.background_work_registry, "report", _raise)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    with pytest.raises(HTTPException) as exc:
        _run(handlers["report"][0](bwc.ReportPayload(local_id="job", label="y")))
    assert exc.value.status_code == 400
    assert "bad label" in exc.value.detail


def test_update_returns_applied(monkeypatch):
    captured = {}

    def _update(item_id, **kw):
        captured["item_id"] = item_id
        captured["kw"] = kw
        return True

    monkeypatch.setattr(bwc.background_work_registry, "update", _update)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    result = _run(handlers["update"][0](
        bwc.UpdatePayload(local_id="job", label="new", detail="d2", phase="done", progress={"x": 2})))
    assert result == {"applied": True}
    assert captured["item_id"] == f"{background_work.OWNER_EXTENSION}:ext-1:job"
    assert captured["kw"]["label"] == "new"


def test_update_translates_rejection_to_400(monkeypatch):
    def _raise(*a, **k):
        raise BackgroundWorkRejected("stale")
    monkeypatch.setattr(bwc.background_work_registry, "update", _raise)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    with pytest.raises(HTTPException) as exc:
        _run(handlers["update"][0](bwc.UpdatePayload(local_id="job", label="y")))
    assert exc.value.status_code == 400


def test_finish_returns_applied(monkeypatch):
    captured = {}

    def _finish(item_id, **kw):
        captured["item_id"] = item_id
        captured["kw"] = kw
        return False

    monkeypatch.setattr(bwc.background_work_registry, "finish", _finish)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    result = _run(handlers["finish"][0](
        bwc.FinishPayload(local_id="job", status=background_work.STATUS_FAILED, error="boom")))
    assert result == {"applied": False}
    assert captured["item_id"] == f"{background_work.OWNER_EXTENSION}:ext-1:job"
    assert captured["kw"]["status"] == background_work.STATUS_FAILED
    assert captured["kw"]["error"] == "boom"


def test_finish_translates_rejection_to_400(monkeypatch):
    def _raise(*a, **k):
        raise BackgroundWorkRejected("nope")
    monkeypatch.setattr(bwc.background_work_registry, "finish", _raise)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    with pytest.raises(HTTPException) as exc:
        _run(handlers["finish"][0](bwc.FinishPayload(local_id="job")))
    assert exc.value.status_code == 400


def test_dismiss_returns_applied(monkeypatch):
    captured = {}

    def _dismiss(item_id):
        captured["item_id"] = item_id
        return True

    monkeypatch.setattr(bwc.background_work_registry, "dismiss", _dismiss)
    caller = ContextVar("caller", default="ext-1")
    handlers = _make_handlers(caller)
    result = _run(handlers["dismiss"][0](bwc.DismissPayload(local_id="job")))
    assert result == {"applied": True}
    assert captured["item_id"] == f"{background_work.OWNER_EXTENSION}:ext-1:job"


# --- registration ---------------------------------------------------------

def test_register_background_work_binds_four_handlers():
    caller = ContextVar("caller", default="ext-1")
    bound = []

    def register(family, name, payload_cls, fn):
        bound.append((family, name, payload_cls.__name__, fn))

    bwc.register_background_work(register, caller)
    assert bound == [
        ("background-work", "report", "ReportPayload", bound[0][3]),
        ("background-work", "update", "UpdatePayload", bound[1][3]),
        ("background-work", "finish", "FinishPayload", bound[2][3]),
        ("background-work", "dismiss", "DismissPayload", bound[3][3]),
    ]
