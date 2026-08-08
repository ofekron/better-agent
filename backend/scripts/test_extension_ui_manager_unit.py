"""Dedicated pytest owner for extension_ui_manager.

The prior owner (scripts/test_extension_ui_manager.py) is a standalone
__main__ script, so pytest collects 0 items and the module was effectively
ownerless at the pytest tier. This owner drives every callable and branch
hermetically: pure helpers directly, the async reconcile/mutate paths via
asyncio.run, and the publication funnel with a patched coordinator.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

paths.engage_test_home(tempfile.mkdtemp(prefix="ext-ui-mgr-unit-"))

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

import event_bus  # noqa: E402
import extension_store  # noqa: E402
import extension_ui_manager as eum  # noqa: E402
import orchestrator  # noqa: E402
from event_bus import BusEvent  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home_and_bus(tmp_path):
    paths.engage_test_home(str(tmp_path))
    paths.reset_home_cache()
    yield
    event_bus.bus.unsubscribe(eum._SUBSCRIPTION_NAME)


def _entry(extension_id, **fields):
    base = {"extension_id": extension_id}
    base.update(fields)
    return base


def _mutated_event(payload=None):
    return BusEvent(
        type=eum.EXTENSION_MUTATED,
        root_id="",
        sid="",
        payload=dict(payload or {}),
        persist=False,
    )


class _FakeCoordinator:
    def __init__(self, *, raise_runtime=False):
        self.calls = []
        self._raise = raise_runtime

    def schedule_global(self, topic, payload, *, loop=None):
        self.calls.append((topic, payload, loop))
        if self._raise:
            raise RuntimeError("broadcast loop closed")


# -- module constants -------------------------------------------------------


def test_topic_and_fact_constants():
    assert eum.EXTENSION_MUTATED == "extension.mutated"
    assert eum.UI_MODULES_CHANGED == "extension.ui.frontend_modules"
    assert eum._SUBSCRIPTION_NAME == "extension_ui_manager.on_extension_mutated"


# -- construction -----------------------------------------------------------


def test_init_defaults():
    mgr = eum.ExtensionUIManager()
    assert mgr._published == {}
    assert mgr._main_loop is None
    assert mgr._reconcile_lock is None
    assert mgr.loop is None  # not started


def test_module_singleton_is_an_instance():
    assert isinstance(eum.manager, eum.ExtensionUIManager)


# -- pure helpers -----------------------------------------------------------


def test_snapshot_builds_deterministic_json_blob_per_entry():
    entry = _entry("a", v=1, name="A")
    snap = eum._snapshot([entry, _entry("b", v=2)])
    assert set(snap) == {"a", "b"}
    assert snap["a"] == json.dumps(
        entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def test_snapshot_skips_empty_missing_or_falsy_extension_id():
    snap = eum._snapshot(
        [
            _entry("a", v=1),
            _entry("", v=2),  # empty -> skip
            {"v": 3},  # missing -> "" -> skip
            _entry(None, v=4),  # None -> "" -> skip
            _entry(0, v=5),  # 0 falsy -> `or ""` -> "" -> skip
        ]
    )
    # `entry.get("extension_id") or ""` collapses empty/missing/None/0 to ""
    assert set(snap) == {"a"}


def test_changed_ids_detects_added_changed_and_removed():
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "CHANGED", "d": "4"}
    assert eum._changed_ids(before, after) == {"b", "c", "d"}


def test_changed_ids_empty_when_identical():
    assert eum._changed_ids({"a": "1"}, {"a": "1"}) == set()


def test_changed_ids_all_new_when_before_empty():
    assert eum._changed_ids({}, {"a": "1", "b": "2"}) == {"a", "b"}


# -- _read_entrypoints ------------------------------------------------------


def test_read_entrypoints_delegates_to_extension_store(monkeypatch):
    payload = [_entry("a"), _entry("b")]
    monkeypatch.setattr(extension_store, "frontend_entrypoints", lambda: payload)
    assert eum._read_entrypoints() == payload


# -- bind / on_stopping -----------------------------------------------------


def test_bind_captures_main_loop_and_subscribes():
    mgr = eum.ExtensionUIManager()
    fake_loop = object()
    mgr.bind(fake_loop)
    assert mgr._main_loop is fake_loop
    # exactly one subscription was registered under our name
    assert event_bus.bus.unsubscribe(eum._SUBSCRIPTION_NAME) == 1


def test_bind_is_idempotent_keeps_single_subscription():
    mgr = eum.ExtensionUIManager()
    mgr.bind(object())
    mgr.bind(object())  # unsubscribe-then-subscribe keeps it single
    assert event_bus.bus.unsubscribe(eum._SUBSCRIPTION_NAME) == 1


def test_on_stopping_unsubscribes():
    mgr = eum.ExtensionUIManager()
    mgr.bind(object())
    mgr.on_stopping()
    assert event_bus.bus.unsubscribe(eum._SUBSCRIPTION_NAME) == 0


# -- _reconcile -------------------------------------------------------------


def test_reconcile_publishes_delta_when_changed(monkeypatch):
    entries = [_entry("a", v=1), _entry("b", v=2)]
    monkeypatch.setattr(extension_store, "frontend_entrypoints", lambda: entries)
    mgr = eum.ExtensionUIManager()
    sent = []
    mgr._broadcast = lambda payload: sent.append(payload)  # type: ignore[assignment]
    asyncio.run(mgr._reconcile())
    assert mgr._published == eum._snapshot(entries)
    assert len(sent) == 1
    assert sent[0]["entrypoints"] == entries
    assert sorted(sent[0]["changed_extension_ids"]) == ["a", "b"]


def test_reconcile_silent_when_projection_unchanged(monkeypatch):
    entries = [_entry("a", v=1)]
    monkeypatch.setattr(extension_store, "frontend_entrypoints", lambda: entries)
    mgr = eum.ExtensionUIManager()
    mgr._published = eum._snapshot(entries)  # already published
    sent = []
    mgr._broadcast = lambda payload: sent.append(payload)  # type: ignore[assignment]
    asyncio.run(mgr._reconcile())
    assert sent == []


# -- _on_extension_mutated --------------------------------------------------


def test_on_extension_mutated_creates_lock_and_reconciles(monkeypatch):
    mgr = eum.ExtensionUIManager()
    assert mgr._reconcile_lock is None
    monkeypatch.setattr(
        extension_store, "frontend_entrypoints", lambda: [_entry("a")]
    )
    mgr._broadcast = lambda payload: None  # type: ignore[assignment]
    asyncio.run(mgr._on_extension_mutated(_mutated_event({"extension_id": "a"})))
    assert mgr._reconcile_lock is not None


def test_on_extension_mutated_reuses_existing_lock(monkeypatch):
    mgr = eum.ExtensionUIManager()
    pre = asyncio.Lock()
    mgr._reconcile_lock = pre
    monkeypatch.setattr(extension_store, "frontend_entrypoints", lambda: [])
    mgr._broadcast = lambda payload: None  # type: ignore[assignment]
    asyncio.run(mgr._on_extension_mutated(_mutated_event()))
    assert mgr._reconcile_lock is pre


def test_on_extension_mutated_broadcasts_empty_on_failure(monkeypatch):
    mgr = eum.ExtensionUIManager()

    def _boom():
        raise RuntimeError("extension store unavailable")

    monkeypatch.setattr(extension_store, "frontend_entrypoints", _boom)
    sent = []
    mgr._broadcast = lambda payload: sent.append(payload)  # type: ignore[assignment]
    asyncio.run(
        mgr._on_extension_mutated(_mutated_event({"extension_id": "a"}))
    )
    assert sent == [{}]


# -- _broadcast -------------------------------------------------------------


def test_broadcast_warns_when_not_bound(caplog):
    mgr = eum.ExtensionUIManager()
    assert mgr._main_loop is None
    with caplog.at_level("WARNING", logger="extension_ui_manager"):
        mgr._broadcast({"entrypoints": []})
    assert any("not bound" in r.message for r in caplog.records)


def test_broadcast_warns_when_loop_closed(caplog):
    mgr = eum.ExtensionUIManager()
    loop = asyncio.new_event_loop()
    loop.close()
    mgr._main_loop = loop
    with caplog.at_level("WARNING", logger="extension_ui_manager"):
        mgr._broadcast({"x": 1})
    assert any("not bound" in r.message for r in caplog.records)


def test_broadcast_noop_when_no_coordinator(monkeypatch):
    mgr = eum.ExtensionUIManager()
    loop = asyncio.new_event_loop()
    mgr._main_loop = loop
    try:
        monkeypatch.setattr(orchestrator, "get_active_coordinator", lambda: None)
        mgr._broadcast({"x": 1})  # no coordinator -> silent return
    finally:
        loop.close()


def test_broadcast_swallows_schedule_runtime_error(monkeypatch):
    mgr = eum.ExtensionUIManager()
    loop = asyncio.new_event_loop()
    mgr._main_loop = loop
    coord = _FakeCoordinator(raise_runtime=True)
    monkeypatch.setattr(orchestrator, "get_active_coordinator", lambda: coord)
    try:
        mgr._broadcast({"x": 1})  # RuntimeError swallowed
    finally:
        loop.close()
    assert coord.calls == [(eum.UI_MODULES_CHANGED, {"x": 1}, loop)]


def test_broadcast_happy_path(monkeypatch):
    mgr = eum.ExtensionUIManager()
    loop = asyncio.new_event_loop()
    mgr._main_loop = loop
    coord = _FakeCoordinator()
    monkeypatch.setattr(orchestrator, "get_active_coordinator", lambda: coord)
    try:
        mgr._broadcast({"entrypoints": [_entry("a")], "changed_extension_ids": ["a"]})
    finally:
        loop.close()
    assert coord.calls == [
        (
            eum.UI_MODULES_CHANGED,
            {"entrypoints": [_entry("a")], "changed_extension_ids": ["a"]},
            loop,
        )
    ]
