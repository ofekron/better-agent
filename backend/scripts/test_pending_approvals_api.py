"""Unit coverage for pending_approvals_api.py.

The router is thin HTTP wiring around the disk-backed `stores.pending_approvals`
store plus two injected coordinator capabilities (the actively-awaited
delegation ids and the approval resolver) and the internal role-authority gate.
This file covers its unit-tier surface end to end: the authority gate (403),
the unconfigured guard (503, including the partial-bind branch), input
validation (400), the store-outcome -> HTTP mapping (404 missing / 410 expired
/ idempotent already_resolved / ok resolved), and the active-delegation filter
on list.

The store is disk I/O (fcntl-locked json) so it is faked at this tier — the
store has its own dedicated coverage. Only `internal_guards` (authority) and
`pending_approvals` (store) are faked, patched at the router's import bindings;
the two configure capabilities are real lambdas.

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python scripts/test_pending_approvals_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from fastapi import HTTPException

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-pending-approvals-api-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pending_approvals_api as pa  # noqa: E402
from pending_approvals_api import (  # noqa: E402
    _required_delegation_id,
    _resolved_or_error,
    configure,
    internal_approve_pending_approval,
    internal_deny_pending_approval,
    internal_list_pending_approvals,
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
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized

    def require_role_internal(self, role: str) -> None:
        if not self.authorized:
            raise HTTPException(status_code=403, detail="invalid internal token")


class FakeStore:
    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.approve_result: tuple[Any, str] = ({"delegation_id": "D1", "status": "approved"}, "ok")
        self.deny_result: tuple[Any, str] = ({"delegation_id": "D1", "status": "denied"}, "ok")
        self.last_cwd: Any = "unset"
        self.last_approve: Any = None
        self.last_deny: Any = None

    def list_pending(self, *, cwd: str | None = None) -> list[dict]:
        self.last_cwd = cwd
        return list(self.pending)

    def approve(self, delegation_id: str, *, description: str | None = None,
                orchestration_mode: str | None = None) -> tuple[Any, str]:
        self.last_approve = (delegation_id, description, orchestration_mode)
        return self.approve_result

    def deny(self, delegation_id: str) -> tuple[Any, str]:
        self.last_deny = delegation_id
        return self.deny_result


def _reset() -> None:
    """Drop router state + rebind fresh fakes between tests."""
    pa._awaited_delegation_ids = None
    pa._resolve_approval = None
    pa.internal_guards = FakeGuards(authorized=True)
    pa.pending_approvals = FakeStore()


def _expect_http(coro: Any, code: int, name: str) -> None:
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        _check(status == code, name, f"expected {code}, got {status}: {exc!r}")
        return
    _check(False, name, f"expected HTTPException {code}, none raised")


def _bind_caps(*, awaited: list[str] | None = None, resolved: list | None = None) -> tuple[list, list]:
    """configure() with real lambdas; return ([awaited ids], [resolved calls])."""
    awaited_ids = awaited if awaited is not None else ["D1"]
    resolved_calls: list = []
    configure(
        awaited_delegation_ids=(lambda: awaited_ids),
        resolve_approval=(lambda did, rec: resolved_calls.append((did, rec))),
    )
    return awaited_ids, resolved_calls


# --------------------------------------------------------------------------- #
# configure / _require_configured
# --------------------------------------------------------------------------- #
def test_configure_binds_both_caps() -> None:
    _reset()
    awaited, resolved = _bind_caps()
    _check(pa._awaited_delegation_ids is not None and pa._resolve_approval is not None,
           "configure: binds both capabilities")


def test_require_configured_503_both_none() -> None:
    _reset()  # both None
    _expect_http(_noop_require_configured(), 503, "_require_configured: both None -> 503")


async def _noop_require_configured() -> None:
    pa._require_configured()


def test_require_configured_503_partial_resolve_none() -> None:
    _reset()
    # awaited bound, resolve None -> exercises the `or` second-operand branch.
    pa._awaited_delegation_ids = lambda: []
    pa._resolve_approval = None
    _expect_http(_noop_require_configured(), 503, "_require_configured: resolve None -> 503")


# --------------------------------------------------------------------------- #
# _resolved_or_error
# --------------------------------------------------------------------------- #
def test_resolved_or_error_mapping() -> None:
    rec = {"status": "approved", "delegation_id": "D1"}
    # missing -> 404
    try:
        _resolved_or_error(rec, "missing")
        _check(False, "_resolved_or_error: missing -> 404", "no raise")
    except HTTPException as exc:
        _check(exc.status_code == 404, "_resolved_or_error: missing -> 404")
    # expired -> 410
    try:
        _resolved_or_error(rec, "expired")
        _check(False, "_resolved_or_error: expired -> 410", "no raise")
    except HTTPException as exc:
        _check(exc.status_code == 410, "_resolved_or_error: expired -> 410")
    # already_resolved -> idempotent dict
    out = _resolved_or_error(rec, "already_resolved")
    _check(out == {"status": "approved", "record": rec, "idempotent": True},
           "_resolved_or_error: already_resolved -> idempotent dict")
    # ok / unknown -> None (continue)
    _check(_resolved_or_error(rec, "ok") is None, "_resolved_or_error: ok -> None")


# --------------------------------------------------------------------------- #
# _required_delegation_id
# --------------------------------------------------------------------------- #
def test_required_delegation_id() -> None:
    # missing (body falsy branch) -> 400
    try:
        _required_delegation_id({})
        _check(False, "_required_delegation_id: missing -> 400", "no raise")
    except HTTPException as exc:
        _check(exc.status_code == 400, "_required_delegation_id: missing -> 400")
    # present (body truthy branch) -> stripped id
    _check(_required_delegation_id({"delegation_id": "  D9  "}) == "D9",
           "_required_delegation_id: returns stripped id")


# --------------------------------------------------------------------------- #
# internal_list_pending_approvals
# --------------------------------------------------------------------------- #
def test_list_unauthorized_403() -> None:
    _reset()
    pa.internal_guards = FakeGuards(authorized=False)
    _expect_http(internal_list_pending_approvals({"cwd": "/x"}, "tok"), 403,
                 "list: unauthorized -> 403")


def test_list_filters_to_active_delegations() -> None:
    _reset()
    awaited, _ = _bind_caps(awaited=["D1"])
    store = pa.pending_approvals
    store.pending = [
        {"delegation_id": "D1", "status": "pending"},
        {"delegation_id": "D2", "status": "pending"},  # orphan — not awaited
    ]
    # body with cwd (truthy branch of `(body or {})`).
    res = asyncio.run(internal_list_pending_approvals({"cwd": "/x"}, "tok"))
    _check(res == {"approvals": [{"delegation_id": "D1", "status": "pending"}]},
           "list: returns only actively-awaited approvals")
    _check(store.last_cwd == "/x", "list: forwards cwd filter")
    # body=None (falsy branch of `(body or {})`).
    res2 = asyncio.run(internal_list_pending_approvals(None, "tok"))
    _check(res2 == {"approvals": [{"delegation_id": "D1", "status": "pending"}]},
           "list: body=None still works (cwd=None)")
    _check(store.last_cwd is None, "list: cwd=None when body absent")


# --------------------------------------------------------------------------- #
# internal_approve_pending_approval
# --------------------------------------------------------------------------- #
def test_approve_unauthorized_403() -> None:
    _reset()
    pa.internal_guards = FakeGuards(authorized=False)
    _expect_http(internal_approve_pending_approval({"delegation_id": "D1"}, "tok"), 403,
                 "approve: unauthorized -> 403")


def test_approve_missing_delegation_id_400() -> None:
    _reset()
    _bind_caps()
    # body={} exercises the falsy branch of `body or {}` on line 99.
    _expect_http(internal_approve_pending_approval({}, "tok"), 400,
                 "approve: missing delegation_id -> 400")


def test_approve_store_outcomes() -> None:
    _reset()
    awaited, resolved = _bind_caps()
    store = pa.pending_approvals
    body = {"delegation_id": "D1", "description": "desc", "orchestration_mode": "team"}

    # missing -> 404
    store.approve_result = (None, "missing")
    _expect_http(internal_approve_pending_approval(body, "tok"), 404, "approve: store missing -> 404")
    # expired -> 410
    store.approve_result = ({"delegation_id": "D1", "status": "pending"}, "expired")
    _expect_http(internal_approve_pending_approval(body, "tok"), 410, "approve: store expired -> 410")
    # already_resolved -> idempotent (no resolve call)
    rec = {"delegation_id": "D1", "status": "approved"}
    store.approve_result = (rec, "already_resolved")
    res = asyncio.run(internal_approve_pending_approval(body, "tok"))
    _check(res == {"status": "approved", "record": rec, "idempotent": True},
           "approve: already_resolved -> idempotent (no resolve)")
    _check(resolved == [], "approve: already_resolved does NOT call resolve_approval")
    # ok -> resolve called, returns approved
    rec2 = {"delegation_id": "D1", "status": "approved", "approved_orchestration_mode": "team"}
    store.approve_result = (rec2, "ok")
    res2 = asyncio.run(internal_approve_pending_approval(body, "tok"))
    _check(res2 == {"status": "approved", "record": rec2}, "approve: ok -> {status:approved, record}")
    _check(resolved == [("D1", rec2)], "approve: ok calls resolve_approval(did, rec)")
    _check(store.last_approve == ("D1", "desc", "team"), "approve: forwards description + orchestration_mode")


# --------------------------------------------------------------------------- #
# internal_deny_pending_approval
# --------------------------------------------------------------------------- #
def test_deny_unauthorized_403() -> None:
    _reset()
    pa.internal_guards = FakeGuards(authorized=False)
    _expect_http(internal_deny_pending_approval({"delegation_id": "D1"}, "tok"), 403,
                 "deny: unauthorized -> 403")


def test_deny_missing_delegation_id_400() -> None:
    _reset()
    _bind_caps()
    _expect_http(internal_deny_pending_approval({}, "tok"), 400,
                 "deny: missing delegation_id -> 400")


def test_deny_store_outcomes() -> None:
    _reset()
    awaited, resolved = _bind_caps()
    store = pa.pending_approvals

    # missing -> 404
    store.deny_result = (None, "missing")
    _expect_http(internal_deny_pending_approval({"delegation_id": "D1"}, "tok"), 404,
                 "deny: store missing -> 404")
    # already_resolved -> idempotent early return (no resolve call) [deny `return early` path]
    rec_ar = {"delegation_id": "D1", "status": "denied"}
    store.deny_result = (rec_ar, "already_resolved")
    res_ar = asyncio.run(internal_deny_pending_approval({"delegation_id": "D1"}, "tok"))
    _check(res_ar == {"status": "denied", "record": rec_ar, "idempotent": True},
           "deny: already_resolved -> idempotent (no resolve)")
    _check(resolved == [], "deny: already_resolved does NOT call resolve_approval")
    # ok -> resolve called, returns denied
    rec = {"delegation_id": "D1", "status": "denied"}
    store.deny_result = (rec, "ok")
    res = asyncio.run(internal_deny_pending_approval({"delegation_id": "D1"}, "tok"))
    _check(res == {"status": "denied", "record": rec}, "deny: ok -> {status:denied, record}")
    _check(resolved == [("D1", rec)], "deny: ok calls resolve_approval(did, rec)")
    _check(store.last_deny == "D1", "deny: forwards delegation_id")


if __name__ == "__main__":
    tests = [
        test_configure_binds_both_caps,
        test_require_configured_503_both_none,
        test_require_configured_503_partial_resolve_none,
        test_resolved_or_error_mapping,
        test_required_delegation_id,
        test_list_unauthorized_403,
        test_list_filters_to_active_delegations,
        test_approve_unauthorized_403,
        test_approve_missing_delegation_id_400,
        test_approve_store_outcomes,
        test_deny_unauthorized_403,
        test_deny_missing_delegation_id_400,
        test_deny_store_outcomes,
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
