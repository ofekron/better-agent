#!/usr/bin/env python3
"""Dedicated unit coverage for backend/extension_incident_outbox.py.

extension_incident_outbox.py is the durable buffer that persists node-side
extension performance incidents (slow backend calls / backend timeouts) to
``ba_home()/node-extension-incidents/outbox.json`` until the extension layer
acks them. It owns the schema-version gate, a TTL expiry filter, a hard
capacity cap, and an idempotent ack that dedupes by incident_id.

The only same-name owner, test_extension_incident_delivery.py, is a standalone
``__main__`` script (pytest collects 0 items), and the module has no other
test importer, so it was effectively pytest-ownerless at the unit tier
(0% beyond import-time). This file drives every callable + branch
hermetically against an isolated BETTER_AGENT_HOME tempdir. The module is a
real-threading + real-filesystem store; collaborators (json_store, paths) are
exercised directly against the tempdir. No real state is ever touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-eio-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(str(_TEST_HOME))

import extension_incident_outbox as outbox  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_outbox():
    """Reset the durable outbox file before each test.

    All tests share one isolated tempdir + the module's threading lock, so
    persisted incidents would leak between tests and make ordering matter
    (LESSON 53). Wipe the on-disk file before every test; tests that need a
    seeded file re-seed it in their own body.
    """
    path = _outbox_path()
    if path.exists():
        path.unlink()
    yield


# --- helpers -----------------------------------------------------------------


def _outbox_path() -> Path:
    # The module's own resolution is the single source of truth for where the
    # file lives under the effective (conftest-engaged, isolated) home; do not
    # reconstruct it from _TEST_HOME, which can diverge under env caching.
    return outbox._path()


def _seed_file(payload: dict) -> None:
    """Write an arbitrary JSON payload as the outbox file (bypassing _save)."""
    path = _outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _seed_incidents(items: list) -> None:
    _seed_file({"schema_version": outbox.SCHEMA_VERSION, "incidents": items})


def _valid_incident(occurred_at: float, *, incident_id: str = "inc") -> dict:
    return {
        "incident_id": incident_id,
        "kind": "slow_backend_call",
        "occurred_at": occurred_at,
    }


# --- constants / _empty ------------------------------------------------------


def test_empty_shape():
    empty = outbox._empty()
    assert empty == {"schema_version": outbox.SCHEMA_VERSION, "incidents": []}
    # _empty returns a fresh dict each call (no shared mutable default leak)
    empty["incidents"].append("x")
    assert outbox._empty()["incidents"] == []


def test_home_is_isolated_from_production():
    # The conftest test-home engagement must keep outbox writes out of the
    # real user state root, no matter which temp home won the env resolution.
    from paths import ba_home

    assert str(ba_home()) != str(Path.home() / ".better-claude")
    assert outbox._path().name == "outbox.json"
    assert outbox._path().parent.name == "node-extension-incidents"


# --- _load -------------------------------------------------------------------


def test_load_missing_file_returns_empty():
    assert outbox._load() == outbox._empty()


def test_load_rejects_wrong_schema_version():
    _seed_file({"schema_version": 99, "incidents": [{"x": 1}]})
    assert outbox._load() == outbox._empty()


def test_load_rejects_non_list_incidents():
    _seed_file({"schema_version": outbox.SCHEMA_VERSION, "incidents": "nope"})
    assert outbox._load() == outbox._empty()


def test_load_returns_valid_payload():
    items = [_valid_incident(1000.0)]
    _seed_incidents(items)
    assert outbox._load()["incidents"] == items


def test_save_round_trips_through_load():
    data = {"schema_version": outbox.SCHEMA_VERSION, "incidents": [_valid_incident(1.0)]}
    outbox._save(data)
    assert outbox._load() == data


# --- _unexpired_incidents ----------------------------------------------------


def test_unexpired_keeps_valid_recent_item():
    now = 10_000.0
    data = {"incidents": [_valid_incident(now - 1.0)]}
    assert outbox._unexpired_incidents(data, now) == [_valid_incident(now - 1.0)]


def test_unexpired_drops_expired_item():
    now = 10_000.0
    expired_at = now - outbox.MAX_PENDING_INCIDENT_AGE_SECONDS - 1.0
    data = {"incidents": [_valid_incident(expired_at)]}
    assert outbox._unexpired_incidents(data, now) == []


def test_unexpired_drops_non_dict_item():
    data = {"incidents": ["junk", 7, None]}
    assert outbox._unexpired_incidents(data, 10_000.0) == []


def test_unexpired_drops_non_numeric_occurred_at():
    data = {"incidents": [{"incident_id": "x", "occurred_at": "soon"}]}
    assert outbox._unexpired_incidents(data, 10_000.0) == []


def test_unexpired_boundary_equal_age_is_kept():
    # occurred_at == cutoff is NOT < cutoff, so it survives.
    now = 10_000.0
    boundary = now - outbox.MAX_PENDING_INCIDENT_AGE_SECONDS
    data = {"incidents": [_valid_incident(boundary)]}
    assert outbox._unexpired_incidents(data, now) == [_valid_incident(boundary)]


# --- _load_unexpired ---------------------------------------------------------


def test_load_unexpired_no_rewrite_when_all_valid(monkeypatch):
    calls = []
    monkeypatch.setattr(outbox, "_save", lambda data: calls.append(data))
    _seed_incidents([_valid_incident(10_000.0), _valid_incident(9_999.0)])
    data = outbox._load_unexpired(10_000.0)
    assert len(data["incidents"]) == 2
    assert calls == []  # nothing expired -> no persistence rewrite


def test_load_unexpired_rewrites_when_some_expired():
    now = 10_000.0
    kept = _valid_incident(now - 1.0, incident_id="keep")
    expired = _valid_incident(now - outbox.MAX_PENDING_INCIDENT_AGE_SECONDS - 1.0, incident_id="gone")
    _seed_incidents([kept, expired])
    data = outbox._load_unexpired(now)
    assert [i["incident_id"] for i in data["incidents"]] == ["keep"]
    # The rewrite actually persisted the filtered set.
    assert [i["incident_id"] for i in outbox._load()["incidents"]] == ["keep"]


# --- enqueue -----------------------------------------------------------------


def test_enqueue_rejects_unknown_kind():
    with pytest.raises(ValueError):
        outbox.enqueue(kind="not_a_real_kind", extension_id="ext", activation_id="act", elapsed_seconds=1.0)


@pytest.mark.parametrize("kind", sorted(outbox._KINDS))
def test_enqueue_both_kinds(kind):
    fact = outbox.enqueue(kind=kind, extension_id="ext", activation_id="act", elapsed_seconds=0.5)
    assert fact["kind"] == kind
    assert any(i["incident_id"] == fact["incident_id"] for i in outbox.pending())


def test_enqueue_coerces_types_and_defaults_occurred_at():
    fact = outbox.enqueue(
        kind="backend_timeout",
        extension_id=123,
        activation_id=456,
        elapsed_seconds="2.5",
    )
    assert fact["extension_id"] == "123"
    assert fact["activation_id"] == "456"
    assert fact["elapsed_seconds"] == 2.5
    assert isinstance(fact["occurred_at"], float)
    assert isinstance(fact["incident_id"], str) and fact["incident_id"]
    assert "path" not in fact


def test_enqueue_unique_incident_ids():
    a = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    b = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    assert a["incident_id"] != b["incident_id"]


def test_enqueue_honors_explicit_occurred_at_and_path():
    fact = outbox.enqueue(
        kind="slow_backend_call",
        extension_id="e",
        activation_id="a",
        elapsed_seconds=1.0,
        path="/some/path",
        occurred_at=4242.0,
    )
    assert fact["occurred_at"] == 4242.0
    assert fact["path"] == "/some/path"


def test_enqueue_raises_when_outbox_full(monkeypatch):
    outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    monkeypatch.setattr(outbox, "MAX_PENDING_INCIDENTS", len(outbox.pending()))
    with pytest.raises(outbox.IncidentOutboxFull):
        outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)


def test_enqueue_at_capacity_boundary_is_accepted(monkeypatch):
    # Capacity check is strictly >=, so exactly MAX items already present rejects;
    # one below MAX accepts. Verified by setting MAX to current+1.
    outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    monkeypatch.setattr(outbox, "MAX_PENDING_INCIDENTS", len(outbox.pending()) + 1)
    fact = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    assert fact["incident_id"]


def test_enqueue_drops_expired_before_counting(monkeypatch):
    # An expired incident is filtered out by _load_unexpired BEFORE the capacity
    # check, so it must not count toward MAX. Seed one stale incident and cap
    # MAX at 1: if the stale row were counted, enqueue would hit IncidentOutboxFull;
    # because it is dropped first, the new fact is accepted.
    now = 10_000.0
    _seed_incidents([_valid_incident(now - outbox.MAX_PENDING_INCIDENT_AGE_SECONDS - 1.0)])
    monkeypatch.setattr(outbox, "MAX_PENDING_INCIDENTS", 1)
    fact = outbox.enqueue(
        kind="slow_backend_call",
        extension_id="e",
        activation_id="a",
        elapsed_seconds=1.0,
        occurred_at=now,
    )
    assert fact["incident_id"]


# --- pending -----------------------------------------------------------------


def test_pending_returns_defensive_copies():
    fact = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    snapshot = outbox.pending()
    assert snapshot[0]["incident_id"] == fact["incident_id"]
    snapshot[0]["kind"] = "tampered"
    assert outbox.pending()[0]["kind"] == "slow_backend_call"


def test_pending_empty_on_fresh_home():
    assert outbox.pending() == []


# --- ack ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", None])
def test_ack_rejects_falsy_id(bad):
    assert outbox.ack(bad) is False


def test_ack_unknown_id_returns_false():
    assert outbox.ack("does-not-exist") is False


def test_ack_removes_matching_incident():
    fact = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    other = outbox.enqueue(kind="backend_timeout", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    assert outbox.ack(fact["incident_id"]) is True
    ids = {i["incident_id"] for i in outbox.pending()}
    assert fact["incident_id"] not in ids
    assert other["incident_id"] in ids


def test_ack_is_idempotent_second_call_returns_false():
    fact = outbox.enqueue(kind="slow_backend_call", extension_id="e", activation_id="a", elapsed_seconds=1.0)
    assert outbox.ack(fact["incident_id"]) is True
    assert outbox.ack(fact["incident_id"]) is False
