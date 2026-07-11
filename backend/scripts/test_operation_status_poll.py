"""Durable operation-id poll contract (plan Phase 1, item F).

Locks:
- ask/delegation status stores stay one implementation (thin bindings
  over operation_status_store) with unchanged on-disk layout.
- `operation_status` normalizes found/status and fails closed on
  unknown kinds and unsafe ids.
- RuntimeClient exposes the poll contract without a live coordinator.
- The internal REST endpoint maps validation errors to 400.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-operation-status-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_status_store
import delegation_status_store
from operation_status_store import operation_status
from runtime_client import runtime


def test_ask_store_roundtrip_layout_unchanged():
    ask_status_store.write_status("ask_abc-1", lifecycle_msg_id="lm1")
    path = ask_status_store.status_path("ask_abc-1")
    assert path.parent.name == "ask-status"
    assert path.exists()
    rec = ask_status_store.read_status("ask_abc-1")
    assert rec == {"lifecycle_msg_id": "lm1"}
    ask_status_store.delete_status("ask_abc-1")
    assert ask_status_store.read_status("ask_abc-1") is None


def test_delegation_store_roundtrip_layout_unchanged():
    delegation_status_store.write_status("del_xyz", status="queued")
    path = delegation_status_store.status_path("del_xyz")
    assert path.parent.name == "delegate-status"
    rec = delegation_status_store.read_status("del_xyz")
    assert rec == {"status": "queued"}


def test_operation_status_not_found():
    out = operation_status("ask", "ask_never_written")
    assert out == {
        "kind": "ask",
        "operation_id": "ask_never_written",
        "found": False,
        "status": "unknown",
        "record": None,
    }


def test_operation_status_ask_in_flight_then_complete():
    ask_status_store.write_status("ask_flow", lifecycle_msg_id="lm2")
    assert operation_status("ask", "ask_flow")["status"] == "in_flight"
    ask_status_store.write_status("ask_flow", result={"text": "done"})
    out = operation_status("ask", "ask_flow")
    assert out["found"] is True
    assert out["status"] == "complete"
    assert out["record"]["result"] == {"text": "done"}


def test_operation_status_delegation_uses_record_status():
    delegation_status_store.write_status("del_run", status="running")
    assert operation_status("delegation", "del_run")["status"] == "running"


def test_operation_status_fails_closed():
    for kind, op_id in (
        ("nope", "ask_x"),
        ("", "ask_x"),
        ("ask", ""),
        ("ask", "../escape"),
        ("ask", "a/b"),
    ):
        try:
            operation_status(kind, op_id)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {(kind, op_id)!r}")


def test_runtime_client_poll_needs_no_coordinator():
    ask_status_store.write_status("ask_rc", result={"text": "ok"})
    delegation_status_store.write_status("del_rc", status="complete")
    assert runtime.operation_status("ask", "ask_rc")["status"] == "complete"
    assert runtime.operation_status("delegation", "del_rc")["status"] == "complete"


def test_internal_endpoint_maps_validation_to_400():
    import asyncio

    import main
    from fastapi import HTTPException

    # Self-contained: write the record here so the __main__ runner's
    # alphabetical order (this test before the RuntimeClient poll test)
    # does not matter.
    ask_status_store.write_status("ask_rc", result={"text": "ok"})
    original = main._internal_authority_is_valid
    main._internal_authority_is_valid = lambda: True
    try:
        ok = asyncio.run(main.internal_operation_status(
            {"kind": "ask", "operation_id": "ask_rc"}, x_internal_token="t",
        ))
        assert ok["status"] == "complete"
        for bad in (
            {"kind": "nope", "operation_id": "x"},
            {"kind": "ask", "operation_id": "../escape"},
        ):
            try:
                asyncio.run(main.internal_operation_status(
                    bad, x_internal_token="t",
                ))
            except HTTPException as exc:
                assert exc.status_code == 400
                continue
            raise AssertionError(f"expected 400 for {bad!r}")
    finally:
        main._internal_authority_is_valid = original


def test_internal_endpoint_denies_without_authority():
    import asyncio

    import main
    from fastapi import HTTPException

    original = main._internal_authority_is_valid
    main._internal_authority_is_valid = lambda: False
    try:
        asyncio.run(main.internal_operation_status(
            {"kind": "ask", "operation_id": "ask_rc"}, x_internal_token="t",
        ))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected 403 without internal authority")
    finally:
        main._internal_authority_is_valid = original


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
