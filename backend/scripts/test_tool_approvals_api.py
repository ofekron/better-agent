"""Unit coverage for tool_approvals_api.py.

The router is thin HTTP wiring around the in-process `tool_approval.registry`
plus an injected broadcast coordinator and the internal-token authority gate.
This file covers its unit-tier surface end to end: the authority gate (403),
input validation (400), the unconfigured-coordinator guard (503), object-level
authz on decide (404 for unknown + cross-session), the blocking
request→decision handshake (real registry Future), and the pending-card
rehydration read.

The real `tool_approval.registry` is exercised directly — it is pure
in-process logic with no I/O, so using it (rather than mocking it) tests the
actual handshake. Only `internal_guards` (authority) and the coordinator
(broadcast) are faked, patched at the router's import bindings.

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python scripts/test_tool_approvals_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-tool-approvals-api-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import tool_approvals_api  # noqa: E402
import tool_approval  # noqa: E402
from tool_approvals_api import (  # noqa: E402
    configure,
    decide_tool_approval,
    internal_tool_approval_request,
    list_pending_tool_approvals,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[bool, str]] = []


def _check(cond: bool, name: str, detail: str = "") -> None:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    _results.append((cond, name))


# --------------------------------------------------------------------------- #
# Collaborator fakes
# --------------------------------------------------------------------------- #
class FakeGuards:
    def __init__(self, valid: bool = True) -> None:
        self._valid = valid

    def authority_is_valid(self) -> bool:
        return self._valid


class FakeCoordinator:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, dict, str]] = []

    async def broadcast_session(
        self, sid: str, event: str, payload: dict, *, source: str = ""
    ) -> None:
        self.broadcasts.append((sid, event, payload, source))


def _reset() -> None:
    """Drop router + registry state between tests."""
    tool_approvals_api._coordinator_ref = None
    tool_approval.registry._pending.clear()


def _create(**kw: Any) -> "tool_approval.ToolApproval":
    """registry.create() builds a Future, so it needs a running loop."""
    return asyncio.run(_create_async(**kw))


async def _create_async(**kw: Any) -> "tool_approval.ToolApproval":
    kw.setdefault("run_id", "")
    kw.setdefault("provider_kind", "")
    kw.setdefault("tool_name", "")
    kw.setdefault("summary", {})
    return tool_approval.registry.create(**kw)


def _expect_http(coro: Any, code: int, name: str) -> None:
    """Run an async handler to completion and assert it raised HTTPException(code)."""
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        _check(status == code, name, f"expected {code}, got {status}: {exc!r}")
        return
    _check(False, name, f"expected HTTPException {code}, none raised")


# --------------------------------------------------------------------------- #
# internal_tool_approval_request
# --------------------------------------------------------------------------- #
def test_request_403_invalid_authority() -> None:
    _reset()
    tool_approvals_api.internal_guards = FakeGuards(valid=False)
    _expect_http(
        internal_tool_approval_request({"app_session_id": "S1"}, "tok"),
        403,
        "request: invalid internal token -> 403",
    )


def test_request_400_missing_session() -> None:
    _reset()
    tool_approvals_api.internal_guards = FakeGuards(valid=True)
    # body={} exercises the `body or {}` falsy branch on line 50.
    _expect_http(internal_tool_approval_request({}, "tok"), 400, "request: missing app_session_id -> 400")


def test_request_503_unconfigured_summary_nondict() -> None:
    _reset()
    tool_approvals_api.internal_guards = FakeGuards(valid=True)
    # Non-dict summary exercises the `else {}` branch on line 59; the handler
    # reaches create() then hits the unconfigured _coordinator() -> 503.
    _expect_http(
        internal_tool_approval_request(
            {"app_session_id": "S1", "summary": "not-a-dict"}, "tok"
        ),
        503,
        "request: unconfigured coordinator -> 503 (summary coerced to {})",
    )
    # create() ran before the 503, leaving a dangling record — clear it.
    tool_approval.registry._pending.clear()


def test_request_success_handshake() -> None:
    _reset()
    tool_approvals_api.internal_guards = FakeGuards(valid=True)
    coord = FakeCoordinator()
    configure(coordinator=coord)

    body = {
        "app_session_id": "S1",
        "run_id": "R1",
        "provider_kind": "claude",
        "tool_name": "Bash",
        "summary": {"cmd": "ls"},  # dict branch of line 59; truthy body branch of line 50
    }

    async def drive() -> None:
        task = asyncio.ensure_future(internal_tool_approval_request(body, "tok"))
        # Wait for the "requested" broadcast (carries approval_id from public_view).
        for _ in range(500):
            if coord.broadcasts:
                break
            await asyncio.sleep(0.001)
        _check(bool(coord.broadcasts), "request: broadcast tool_approval_requested fired")
        sid, event, payload, source = coord.broadcasts[0]
        _check(sid == "S1" and event == "tool_approval_requested" and source == "tool_approval",
               "request: requested broadcast args")
        approval_id = payload["approval_id"]

        # Resolve the blocked request via the decide handler (approved=True).
        decide = await decide_tool_approval("S1", approval_id, {"approved": True})
        _check(decide == {"ok": True}, "request: decide handler ok=True")

        result = await task
        _check(result.get("approved") is True and result.get("approval_id") == approval_id,
               "request: returns approved=True + approval_id")
        # Resolved broadcast emitted after the future resolved.
        resolved = [b for b in coord.broadcasts if b[1] == "tool_approval_resolved"]
        _check(len(resolved) == 1 and resolved[0][2] == {"approval_id": approval_id, "approved": True},
               "request: broadcast tool_approval_resolved payload")
        # Record cleaned up after await_decision returns.
        _check(tool_approval.registry.get(approval_id) is None, "request: record popped after resolution")

    asyncio.run(drive())


# --------------------------------------------------------------------------- #
# decide_tool_approval
# --------------------------------------------------------------------------- #
def test_decide_404_unknown() -> None:
    _reset()
    _expect_http(decide_tool_approval("S1", "does-not-exist", {}), 404, "decide: unknown id -> 404")


def test_decide_404_cross_session() -> None:
    _reset()
    rec = _create(app_session_id="S1")
    _expect_http(decide_tool_approval("S2", rec.approval_id, {}), 404, "decide: cross-session id -> 404")


def test_decide_success_denied_empty_body() -> None:
    _reset()
    rec = _create(app_session_id="S1")
    # body={} exercises the falsy branch of `(body or {})` on line 89; absent
    # "approved" key resolves to False (deny).
    res = asyncio.run(decide_tool_approval("S1", rec.approval_id, {}))
    _check(res == {"ok": True}, "decide: deny (no approved key) -> ok=True")
    _check(rec.future.result() is False, "decide: future resolved False")


# --------------------------------------------------------------------------- #
# list_pending_tool_approvals
# --------------------------------------------------------------------------- #
def test_list_pending() -> None:
    _reset()
    rec = _create(app_session_id="S1", run_id="R", provider_kind="claude", tool_name="Bash", summary={"cmd": "ls"})
    # Different session -> empty.
    other = asyncio.run(list_pending_tool_approvals("S2"))
    _check(other == {"approvals": []}, "list_pending: other session empty")
    mine = asyncio.run(list_pending_tool_approvals("S1"))
    _check(
        mine == {"approvals": [tool_approval.registry.public_view(rec)]},
        "list_pending: returns pending approval via public_view",
    )


# --------------------------------------------------------------------------- #
# configure / _coordinator
# --------------------------------------------------------------------------- #
def test_configure_binds_coordinator() -> None:
    coord = object()
    configure(coordinator=coord)
    _check(tool_approvals_api._coordinator_ref is coord, "configure: binds _coordinator_ref")


if __name__ == "__main__":
    tests = [
        test_request_403_invalid_authority,
        test_request_400_missing_session,
        test_request_503_unconfigured_summary_nondict,
        test_request_success_handshake,
        test_decide_404_unknown,
        test_decide_404_cross_session,
        test_decide_success_denied_empty_body,
        test_list_pending,
        test_configure_binds_coordinator,
    ]
    for t in tests:
        t()
    failed = [n for ok, n in _results if not ok]
    print()
    if failed:
        print(f"{FAIL} {len(failed)}/{len(_results)} checks failed:")
        for n in failed:
            print(f"  - {n}")
        sys.exit(1)
    print(f"{PASS} all {len(_results)} checks green ({len(tests)} tests)")
