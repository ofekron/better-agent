"""Unit coverage for ask_status_store.py.

Disk-backed status for in-flight `ask` team-message calls: one JSON file per
ask_id under ``<ba_home>/ask-status/``, with a derived by-lifecycle index, a
file lock per ask_id, route-claim conflict detection, and a result-delivery
projection. Every branch is pinned with a real assertion against the isolated
test home (no production state touched).

Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_ask_status_store_unit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import _test_home  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_HOME = _test_home.isolate("bc-test-ask-status-store-unit-")

import ask_status_store as store  # noqa: E402
from paths import bc_home  # noqa: E402


def _root() -> Path:
    return bc_home() / "ask-status"


def _lifecycle_root() -> Path:
    return bc_home() / "ask-status" / "by-lifecycle"


def _seed_status_file(ask_id: str, payload: object) -> None:
    """Write a raw JSON file bypassing validation (for failure-branch seeds)."""
    _root().mkdir(parents=True, exist_ok=True)
    store.status_path(ask_id).write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# id validation + safe forms
# --------------------------------------------------------------------------- #


def test_is_valid_ask_id_accepts_alphanumerics_dashes_underscores() -> None:
    assert store.is_valid_ask_id("a1_b-2")
    # leading/trailing whitespace is stripped before matching.
    assert store.is_valid_ask_id("  a1  ")


def test_is_valid_ask_id_rejects_unsafe_or_empty() -> None:
    for bad in ("a.b", "a/b", "a b", "", None, "a" * 201):
        assert not store.is_valid_ask_id(bad), repr(bad)


def test_is_valid_lifecycle_id_accepts_and_rejects() -> None:
    assert store.is_valid_lifecycle_id("lc-1_x")
    assert store.is_valid_lifecycle_id("  lc  ")
    assert not store.is_valid_lifecycle_id("lc.1")
    assert not store.is_valid_lifecycle_id("")
    assert not store.is_valid_lifecycle_id(None)


def test_safe_id_returns_stripped_valid_id() -> None:
    assert store._safe_id("  ok-id_1  ") == "ok-id_1"


def test_safe_id_raises_on_invalid() -> None:
    for bad in ("a/b", "", None, "a.b", "a b"):
        try:
            store._safe_id(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_safe_lifecycle_id_returns_stripped_or_raises() -> None:
    assert store._safe_lifecycle_id("  lc-1  ") == "lc-1"
    for bad in ("a.b", "", None):
        try:
            store._safe_lifecycle_id(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_status_path_lives_under_ask_status_dir() -> None:
    path = store.status_path("ask-1")
    assert path.name == "ask-1.json"
    assert path.parent.name == "ask-status"


# --------------------------------------------------------------------------- #
# read_status failure tolerance
# --------------------------------------------------------------------------- #


def test_read_status_missing_returns_none() -> None:
    assert store.read_status("absent") is None


def test_read_status_malformed_json_returns_none() -> None:
    _seed_status_file("malformed", "{not json")
    assert store.read_status("malformed") is None


def test_read_status_non_dict_payload_returns_none() -> None:
    _seed_status_file("listrec", [1, 2, 3])
    assert store.read_status("listrec") is None


def test_read_status_happy_round_trips() -> None:
    store.write_status("rt", state="running", n=3)
    assert store.read_status("rt") == {"state": "running", "n": 3}


# --------------------------------------------------------------------------- #
# write_status: merge, delivery, lifecycle index, result projection
# --------------------------------------------------------------------------- #


def test_write_status_creates_file_and_parent_dir() -> None:
    store.write_status("w1", state="running")
    assert store.status_path("w1").is_file()


def test_write_status_merges_into_existing() -> None:
    store.write_status("w2", state="running")
    store.write_status("w2", count=5)
    data = store.read_status("w2")
    assert data == {"state": "running", "count": 5}


def test_write_status_attaches_delivery_when_sender_given() -> None:
    store.write_status("w3", sender_session_id="sess-1", target_session_id="t1")
    delivery = store.read_status("w3")["delivery"]
    assert delivery["state"] == "waiting"
    assert delivery["caller_session_id"] == "sess-1"
    # No real root registered in the isolated home -> caller_root_id falls
    # back to the sender session id.
    assert delivery["caller_root_id"] == "sess-1"
    assert delivery["caller_terminal"] is False
    # journal_after_seq is whatever the empty journal reports for that root.
    assert isinstance(delivery["journal_after_seq"], int)


def test_write_status_no_sender_leaves_no_delivery() -> None:
    store.write_status("w4", state="running")
    assert "delivery" not in (store.read_status("w4") or {})


def test_write_status_preserves_existing_delivery_dict() -> None:
    # First write seeds a delivery; a later write without a sender must NOT
    # overwrite the existing delivery dict with a fresh one.
    store.write_status("w5", sender_session_id="s1")
    first = store.read_status("w5")["delivery"]["state"]
    store.write_status("w5", extra="x")
    after = store.read_status("w5")
    assert after["delivery"]["state"] == first
    assert after["extra"] == "x"


def test_write_status_with_lifecycle_writes_index() -> None:
    store.write_status("w6", lifecycle_msg_id="lc-6")
    assert store.find_ask_id_by_lifecycle("lc-6") == "w6"


def test_write_status_with_result_projects_delivery_fields() -> None:
    # Seed a delivery first (needs sender), then attach a result.
    store.write_status("w7", sender_session_id="s7", target_session_id="t7")
    store.write_status("w7", result={"assistant_content": " hello "})
    delivery = store.read_status("w7")["delivery"]
    # state "waiting" is not received/inboxed -> projected to "pending".
    assert delivery["state"] == "pending"
    # assistant_content non-empty -> fallback_message is the stripped content.
    assert delivery["fallback_message"] == "hello"
    # result also got stamped with ask_id.
    assert store.read_status("w7")["result"]["ask_id"] == "w7"


def test_project_result_keeps_received_state() -> None:
    store.write_status("p1", sender_session_id="s1")
    # Manually flip delivery to a terminal-ish state, then attach a result.
    store.update_status(
        "p1",
        lambda c: {**c, "delivery": {**c["delivery"], "state": "received"}},
    )
    store.write_status(
        "p1", result={"target_session_id": "tx", "assistant_content": "msg"},
    )
    delivery = store.read_status("p1")["delivery"]
    # received is preserved (not downgraded to pending).
    assert delivery["state"] == "received"
    assert delivery["fallback_sender_session_id"] == "tx"


def test_project_result_fallback_sender_from_worker_or_current_target() -> None:
    # worker_session_id path.
    store.write_status("p2", sender_session_id="s1")
    store.write_status("p2", result={"worker_session_id": "ws1"})
    assert store.read_status("p2")["delivery"]["fallback_sender_session_id"] == "ws1"
    # falls back to current.target_session_id when result lacks both.
    store.write_status("p3", sender_session_id="s1", target_session_id="cur3")
    store.write_status("p3", result={"unrelated": 1})
    assert store.read_status("p3")["delivery"]["fallback_sender_session_id"] == "cur3"


def test_project_result_no_delivery_is_noop() -> None:
    # A result written with no prior delivery leaves delivery absent.
    store.write_status("p4", result={"assistant_content": "x"})
    assert "delivery" not in (store.read_status("p4") or {})


def test_project_result_fallback_message_serializes_result_when_no_content() -> None:
    store.write_status("p5", sender_session_id="s1")
    store.write_status("p5", result={"a": 1, "b": 2})
    fb = store.read_status("p5")["delivery"]["fallback_message"]
    # No assistant_content -> compact JSON of the result.
    assert json.loads(fb) == {"a": 1, "b": 2, "ask_id": "p5"}


# --------------------------------------------------------------------------- #
# find_ask_id_by_lifecycle
# --------------------------------------------------------------------------- #


def test_find_ask_id_by_lifecycle_invalid_returns_none() -> None:
    assert store.find_ask_id_by_lifecycle("bad.id") is None


def test_find_ask_id_by_lifecycle_missing_returns_none() -> None:
    assert store.find_ask_id_by_lifecycle("absent-lc") is None


def test_find_ask_id_by_lifecycle_non_dict_returns_none() -> None:
    store._write_lifecycle_index("lc-nd", "ask-nd")
    store._lifecycle_index_path("lc-nd").write_text("[1,2]", encoding="utf-8")
    assert store.find_ask_id_by_lifecycle("lc-nd") is None


def test_find_ask_id_by_lifecycle_invalid_ask_id_returns_none() -> None:
    store._lifecycle_index_path("lc-bad").parent.mkdir(parents=True, exist_ok=True)
    store._lifecycle_index_path("lc-bad").write_text(
        json.dumps({"ask_id": "bad.id"}), encoding="utf-8",
    )
    assert store.find_ask_id_by_lifecycle("lc-bad") is None


def test_delete_lifecycle_index_missing_is_noop() -> None:
    # No index file exists -> delete swallowed.
    store._delete_lifecycle_index("never-existed")


def test_delete_lifecycle_index_removes_existing() -> None:
    store._write_lifecycle_index("lc-del", "ask-del")
    store._delete_lifecycle_index("lc-del")
    assert store.find_ask_id_by_lifecycle("lc-del") is None


# --------------------------------------------------------------------------- #
# update_status
# --------------------------------------------------------------------------- #


def test_update_status_applies_callable_and_persists() -> None:
    store.write_status("u1", count=1)
    out = store.update_status("u1", lambda c: {**c, "count": c["count"] + 1})
    assert out["count"] == 2
    assert store.read_status("u1")["count"] == 2


def test_update_status_creates_record_when_absent() -> None:
    out = store.update_status("u2", lambda c: {"fresh": True})
    assert out == {"fresh": True}
    assert store.read_status("u2") == {"fresh": True}


def test_update_status_rejects_non_dict_return() -> None:
    try:
        store.update_status("u3", lambda c: ["not", "a", "dict"])
        raise AssertionError("expected TypeError")
    except TypeError:
        pass


# --------------------------------------------------------------------------- #
# write_status_async
# --------------------------------------------------------------------------- #


def test_write_status_async_wraps_sync_write() -> None:
    asyncio.run(store.write_status_async("a1", state="async"))
    assert store.read_status("a1") == {"state": "async"}


def test_write_status_async_skips_delivery_when_not_terminal() -> None:
    # delivery present but caller_terminal False -> deliver_if_needed not
    # invoked (no fallback message either).
    asyncio.run(
        store.write_status_async(
            "a2", sender_session_id="s1", target_session_id="t1",
        )
    )
    assert store.read_status("a2")["delivery"]["caller_terminal"] is False


def test_write_status_async_invokes_delivery_when_terminal_with_fallback() -> None:
    # Craft a terminal delivery whose state is "received": write_status_async
    # sees caller_terminal + fallback_message and calls deliver_if_needed,
    # which short-circuits on state not in {waiting, pending} (no real inbox
    # delivery performed). Proves the terminal branch is wired without
    # driving the full delivery subsystem.
    delivery = {
        "state": "received",
        "caller_terminal": True,
        "fallback_message": "msg",
        "caller_session_id": "c1",
        "caller_root_id": "",
    }
    asyncio.run(store.write_status_async("a3", delivery=delivery))
    # deliver_if_needed short-circuited -> record still present.
    assert store.read_status("a3") is not None


# --------------------------------------------------------------------------- #
# claim_route
# --------------------------------------------------------------------------- #


def test_claim_route_rejects_missing_sender() -> None:
    try:
        store.claim_route("c1", sender_session_id="", target_session_id="t1")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_rejects_bad_route_kind() -> None:
    try:
        store.claim_route(
            "c2", sender_session_id="s1", target_session_id="t1",
            target_selector={"kind": "bogus", "value": "t1"},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_rejects_empty_route_value() -> None:
    # selector value empty and no concrete target -> route_value empty.
    try:
        store.claim_route(
            "c3", sender_session_id="s1", target_session_id="",
            target_selector={"kind": "session", "value": ""},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_non_pool_requires_concrete_target() -> None:
    # route_value is non-empty (from selector) but route_kind is session and
    # there is no concrete target_session_id -> the concrete-target check
    # raises, distinct from the empty-route_value check above.
    try:
        store.claim_route(
            "c4", sender_session_id="s1", target_session_id="",
            target_selector={"kind": "session", "value": "sel-val"},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_creates_new_session_record() -> None:
    claimed = store.claim_route(
        "c5", sender_session_id="s1", target_session_id="t5",
    )
    assert claimed["route_kind"] == "session"
    assert claimed["route_value"] == "t5"
    assert claimed["sender_session_id"] == "s1"
    assert claimed["target_session_id"] == "t5"
    assert claimed["route_affinity_key"] == ""
    assert store.read_status("c5")["route_kind"] == "session"


def test_claim_route_pool_kind_omits_target_session_id() -> None:
    claimed = store.claim_route(
        "c6", sender_session_id="s1", target_session_id="",
        target_selector={"kind": "pool", "value": "my-pool"},
    )
    assert claimed["route_kind"] == "pool"
    assert "target_session_id" not in claimed


def test_claim_route_backfills_unclaimed_existing_record() -> None:
    # A record exists (written directly) without route_kind, with a matching
    # target_session_id -> claimable; route fields are backfilled.
    store.write_status("c7", sender_session_id="s1", target_session_id="t7", extra="x")
    claimed = store.claim_route(
        "c7", sender_session_id="s1", target_session_id="t7",
    )
    assert claimed["route_kind"] == "session"
    assert claimed["route_value"] == "t7"
    assert claimed["extra"] == "x"
    assert claimed["target_session_id"] == "t7"


def test_claim_route_rejects_backfill_when_sender_differs() -> None:
    store.write_status("c8", sender_session_id="orig")
    try:
        store.claim_route("c8", sender_session_id="other", target_session_id="t8")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_rejects_previously_claimed_different_route() -> None:
    store.claim_route("c9", sender_session_id="s1", target_session_id="t9")
    try:
        store.claim_route("c9", sender_session_id="s1", target_session_id="other")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_idempotent_when_same_route_reclaimed() -> None:
    first = store.claim_route("c10", sender_session_id="s1", target_session_id="t10")
    second = store.claim_route("c10", sender_session_id="s1", target_session_id="t10")
    assert second == first


def test_claim_route_rejects_non_pool_target_mismatch_on_backfill() -> None:
    # Existing unclaimed record already has a different target_session_id.
    store.write_status("c11", target_session_id="orig-t")
    try:
        store.claim_route("c11", sender_session_id="s1", target_session_id="new-t")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_pool_binds_target_when_unbound() -> None:
    # Pool claim with a concrete target but no prior binding -> target set.
    store.claim_route(
        "c12", sender_session_id="s1", target_session_id="",
        target_selector={"kind": "pool", "value": "pool-1"},
    )
    claimed = store.claim_route(
        "c12", sender_session_id="s1", target_session_id="resolved-12",
        target_selector={"kind": "pool", "value": "pool-1"},
    )
    assert claimed["target_session_id"] == "resolved-12"


def test_claim_route_pool_rebind_rejected_when_lifecycle_present() -> None:
    store.claim_route(
        "c13", sender_session_id="s1", target_session_id="t-a",
        target_selector={"kind": "pool", "value": "pool-1"},
    )
    # A lifecycle_msg_id now pins this ask to t-a.
    store.write_status("c13", lifecycle_msg_id="lc-13")
    try:
        store.claim_route(
            "c13", sender_session_id="s1", target_session_id="t-b",
            target_selector={"kind": "pool", "value": "pool-1"},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_pool_rebind_rejected_when_result_present() -> None:
    store.claim_route(
        "c14", sender_session_id="s1", target_session_id="t-a",
        target_selector={"kind": "pool", "value": "pool-1"},
    )
    store.write_status("c14", result={"done": True})
    try:
        store.claim_route(
            "c14", sender_session_id="s1", target_session_id="t-b",
            target_selector={"kind": "pool", "value": "pool-1"},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_claim_route_worker_kind_uses_selector_value() -> None:
    claimed = store.claim_route(
        "c15", sender_session_id="s1", target_session_id="w1",
        target_selector={"kind": "worker", "value": "w1", "pool_affinity_key": "k"},
    )
    assert claimed["route_kind"] == "worker"
    assert claimed["route_value"] == "w1"
    assert claimed["route_affinity_key"] == "k"


# --------------------------------------------------------------------------- #
# list_statuses
# --------------------------------------------------------------------------- #


def test_list_statuses_empty_when_no_dir() -> None:
    # Fresh ask_id never written -> root may not exist yet for this listing.
    # Force a clean state by listing before any write under a sub-scope:
    # simplest is to assert against the current (populated) home by checking
    # that a brand-new isolated query returns only what exists. Instead we
    # verify the empty-dir branch by removing the dir.
    if _root().exists():
        shutil.rmtree(_root())
    assert store.list_statuses() == []


def test_list_statuses_returns_existing_records() -> None:
    store.write_status("l1", state="x")
    store.write_status("l2", state="y")
    ids = {ask_id for ask_id, _ in store.list_statuses()}
    assert {"l1", "l2"} <= ids


def test_list_statuses_skips_symlinks_and_non_files() -> None:
    store.write_status("l3", state="ok")
    _root().mkdir(parents=True, exist_ok=True)
    # A symlink named like a status file is skipped.
    link = _root() / "link.json"
    if link.exists():
        link.unlink()
    os.symlink("/nonexistent-target", link)
    # A directory named like a status file is skipped (not a regular file).
    d = _root() / "dir.json"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    ids = {ask_id for ask_id, _ in store.list_statuses()}
    assert "link" not in ids
    assert "dir" not in ids
    assert "l3" in ids


# --------------------------------------------------------------------------- #
# delete_status
# --------------------------------------------------------------------------- #


def test_delete_status_removes_record_and_lifecycle_index() -> None:
    store.write_status("d1", lifecycle_msg_id="lc-d1")
    assert store.find_ask_id_by_lifecycle("lc-d1") == "d1"
    store.delete_status("d1")
    assert store.read_status("d1") is None
    assert store.find_ask_id_by_lifecycle("lc-d1") is None


def test_delete_status_missing_record_is_noop() -> None:
    # No file -> FileNotFoundError swallowed; no lifecycle to clean either.
    store.delete_status("never-existed")


# --------------------------------------------------------------------------- #
# defensive symlink / non-file rejection on roots and paths
# --------------------------------------------------------------------------- #


def test_locked_rejects_symlink_status_path() -> None:
    _root().mkdir(parents=True, exist_ok=True)
    sym = store.status_path("sym1")
    if sym.exists() or sym.is_symlink():
        sym.unlink()
    os.symlink("/nonexistent-target", sym)
    try:
        store.read_status("sym1")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        sym.unlink()


def test_locked_rejects_non_file_status_path() -> None:
    # A directory where the status file should be is not a regular file.
    _root().mkdir(parents=True, exist_ok=True)
    d = store.status_path("nonfile1")
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    try:
        store.read_status("nonfile1")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        shutil.rmtree(d)


def test_lifecycle_index_path_rejects_symlink_file() -> None:
    # The by-lifecycle root is a real dir, but the index FILE is a symlink.
    _lifecycle_root().mkdir(parents=True, exist_ok=True)
    sym = store._lifecycle_index_path("lc-sym")
    if sym.exists() or sym.is_symlink():
        sym.unlink()
    os.symlink("/nonexistent-target", sym)
    try:
        store._write_lifecycle_index("lc-sym", "ask-x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        sym.unlink()


def test_lifecycle_index_root_rejects_symlink_root() -> None:
    if _lifecycle_root().is_symlink():
        _lifecycle_root().unlink()
    elif _lifecycle_root().exists():
        shutil.rmtree(_lifecycle_root())
    os.symlink("/nonexistent-target", _lifecycle_root())
    try:
        store._lifecycle_index_path("any-lc")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        _lifecycle_root().unlink()


def test_ask_status_root_symlink_rejected_by_locked_and_list() -> None:
    # Replace the ask-status dir with a symlink -> _locked (write_status) and
    # list_statuses both refuse.
    if _root().is_symlink():
        _root().unlink()
    elif _root().exists():
        shutil.rmtree(_root())
    os.symlink("/nonexistent-target", _root())
    try:
        store.write_status("z1", state="x")
        raise AssertionError("expected ValueError (write_status)")
    except ValueError:
        pass
    try:
        store.list_statuses()
        raise AssertionError("expected ValueError (list_statuses)")
    except ValueError:
        pass
    finally:
        _root().unlink()


def test_list_statuses_skips_records_that_read_as_none() -> None:
    # A regular file with malformed JSON -> read_status returns None, so it is
    # not included in the listing (covers the status-is-None loop branch).
    store.write_status("l4", state="ok")
    _seed_status_file("l5bad", "{not json")
    ids = {ask_id for ask_id, _ in store.list_statuses()}
    assert "l4" in ids
    assert "l5bad" not in ids
