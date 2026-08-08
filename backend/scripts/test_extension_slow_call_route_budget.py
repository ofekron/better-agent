"""Slow-call quarantine must judge a backend route against the budget its
manifest declares, not against a global floor the host itself ignores."""
from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "scripts"))

import _test_home  # noqa: E402

_TEST_HOME = _test_home.TestHome.acquire("bc-test-slow-call-budget-")
atexit.register(_TEST_HOME.release)

import extension_backend_loader  # noqa: E402
import extension_store  # noqa: E402

_LONG_ROUTE_BUDGET = 360.0
_STRICT_ROUTE_BUDGET = 0.5


def _activation(extension_id: str) -> str:
    """Deterministic activation id in the shape the store validates (32 hex)."""
    return hashlib.sha256(extension_id.encode("utf-8")).hexdigest()[:32]


def _manifest(extension_id: str, backend_timeouts: dict[str, float]) -> dict:
    return {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": "slow-call budget fixture",
        "surfaces": ["backend_feature"],
        "dependencies": [],
        "entrypoints": {
            "backend": "src/main.py",
            "backend_timeouts": backend_timeouts,
            "mcp": [],
            "instructions": [],
            "skills": [],
            "daemons": [],
            "settings": [],
            "frontend_modules": [],
        },
        "permissions": {},
        "marketplace": {
            "product_id": "",
            "subscription_required": False,
            "entitlement_url": "",
        },
    }


def _record(extension_id: str, backend_timeouts: dict[str, float]) -> dict:
    return {
        "manifest": _manifest(extension_id, backend_timeouts),
        "enabled": True,
        "activation_id": _activation(extension_id),
        "installed_at": "2026-07-26T00:00:00+00:00",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "source": {"type": "git", "install_path": "", "commit_sha": ""},
    }


def _seed_store() -> None:
    """Install the records this test judges incidents against, through the
    store's own writer so its fingerprint/projection caches are refreshed
    exactly as a real install refreshes them. Only the package install itself is
    short-circuited; every read path under test is the real one.
    """
    store = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "deleted_extensions": {},
        "extensions": {
            # Mirrors ofek-dev.auto-tagging: one route that synchronously runs a
            # provisioned LLM fork, declared at 360s.
            "probe.declared": _record("probe.declared", {"post-turn": _LONG_ROUTE_BUDGET}),
            # A generous route next to a strict one: the strict route must keep
            # its own budget, including one below the global floor.
            "probe.declared-strict": _record(
                "probe.declared-strict",
                {"post-turn": _LONG_ROUTE_BUDGET, "ping": _STRICT_ROUTE_BUDGET},
            ),
            "probe.undeclared": _record("probe.undeclared", {}),
            "probe.timeouts": _record("probe.timeouts", {"post-turn": _LONG_ROUTE_BUDGET}),
        },
    }
    with extension_store._store_lock():
        extension_store._write_store_unlocked(store)
    for extension_id in (
        "probe.declared",
        "probe.declared-strict",
        "probe.undeclared",
        "probe.timeouts",
    ):
        extension_store._clear_slow_call_history(extension_id)


def _slow(extension_id: str, elapsed: float, path: str) -> list[str]:
    return extension_store.record_slow_backend_call(
        extension_id,
        activation_id=_activation(extension_id),
        elapsed_seconds=elapsed,
        path=path,
    )


def _is_enabled(extension_id: str) -> bool:
    record = extension_store.get_extension(extension_id)
    return bool(record) and record.get("enabled") is True


def test_declared_route_budget_is_not_quarantined_within_budget() -> None:
    _seed_store()
    # Four calls: one more than the quarantine limit, all inside the declared
    # 360s budget. The pre-fix code quarantined on the third.
    for index in range(4):
        disabled = _slow("probe.declared", 39.538, "post-turn")
        if disabled:
            raise AssertionError(
                f"call {index} at 39.538s quarantined a route declared at {_LONG_ROUTE_BUDGET}s: {disabled}"
            )
    if not _is_enabled("probe.declared"):
        raise AssertionError("extension disabled despite staying inside its declared budget")


def test_route_without_declaration_requests_decision_at_global_floor() -> None:
    _seed_store()
    if _slow("probe.undeclared", 4.0, "anything"):
        raise AssertionError("first slow call quarantined")
    if _slow("probe.undeclared", 4.0, "anything"):
        raise AssertionError("second slow call quarantined")
    disabled = _slow("probe.undeclared", 4.0, "anything")
    if disabled != ["probe.undeclared"]:
        raise AssertionError(f"undeclared route not quarantined at the 2.0s floor: {disabled}")
    decision = (extension_store.get_extension("probe.undeclared") or {}).get("pending_health_decision") or {}
    if decision.get("reason") != "repeated_slow_backend_calls":
        raise AssertionError(decision)
    if not _is_enabled("probe.undeclared"):
        raise AssertionError("decision request automatically disabled the extension")


def test_exceeding_a_generous_declared_budget_still_quarantines() -> None:
    _seed_store()
    over = _LONG_ROUTE_BUDGET + 10.0
    if _slow("probe.declared", over, "post-turn"):
        raise AssertionError("first over-budget call quarantined")
    if _slow("probe.declared", over, "post-turn"):
        raise AssertionError("second over-budget call quarantined")
    disabled = _slow("probe.declared", over, "post-turn")
    if disabled != ["probe.declared"]:
        raise AssertionError(f"over-budget route not quarantined: {disabled}")


def test_strict_route_below_the_global_floor_is_still_judged_by_its_budget() -> None:
    _seed_store()
    # "ping" declares 0.5s. Nothing may floor it at EXTENSION_SLOW_CALL_SECONDS:
    # not the store, and not the caller's pre-gate. Its 360s sibling route must
    # not shield it either.
    elapsed = 1.0
    if extension_store.slow_call_threshold(
        {"post-turn": _LONG_ROUTE_BUDGET, "ping": _STRICT_ROUTE_BUDGET}, "ping"
    ) != _STRICT_ROUTE_BUDGET:
        raise AssertionError("shared slow-call threshold ignored a sub-floor declaration")
    for index in range(2):
        if _slow("probe.declared-strict", elapsed, "ping"):
            raise AssertionError(f"call {index} quarantined before the limit")
    disabled = _slow("probe.declared-strict", elapsed, "ping")
    if disabled != ["probe.declared-strict"]:
        raise AssertionError(f"sub-floor declared budget was not enforced: {disabled}")


def test_caller_pre_gate_forwards_the_same_incidents_the_store_counts() -> None:
    """The request-path pre-gate in the loader must use the same per-route
    threshold, or it silently swallows incidents the store would have counted."""
    _seed_store()
    spec = {
        "extension_id": "probe.declared-strict",
        "backend_timeouts": {"post-turn": _LONG_ROUTE_BUDGET, "ping": _STRICT_ROUTE_BUDGET},
    }
    activation_id = _activation("probe.declared-strict")

    async def drive(elapsed: float, path: str) -> None:
        await extension_backend_loader._record_slow_call(
            spec, activation_id, path, elapsed
        )

    # In-budget calls on the generous route reach the store and are dropped there.
    for _ in range(4):
        asyncio.run(drive(39.538, "post-turn"))
    if not _is_enabled("probe.declared-strict"):
        raise AssertionError("in-budget calls on the generous route quarantined")
    # Sub-floor strict route: the pre-gate must forward all three.
    for _ in range(3):
        asyncio.run(drive(1.0, "ping"))
    decision = (extension_store.get_extension("probe.declared-strict") or {}).get("pending_health_decision")
    if not decision:
        raise AssertionError("pre-gate swallowed incidents on a sub-floor declared route")


def test_invoke_backend_attributes_the_route_it_dispatched() -> None:
    """End-to-end through the real dispatch path: the route the request arrived
    on is the route the incident is judged against."""
    _seed_store()
    spec = {
        "extension_id": "probe.declared-strict",
        "backend_timeouts": {"post-turn": _LONG_ROUTE_BUDGET, "ping": _STRICT_ROUTE_BUDGET},
    }
    original_roundtrip = extension_backend_loader._roundtrip

    async def stub_roundtrip(*_args, **_kwargs):
        # Transport stub: a real ~0.6s host-observed delay (the breaker judges
        # host-observed elapsed, not the child's self-reported ASGI time), on
        # a child that additionally reports 1.0s of its own ASGI time — the
        # child's number must NOT be what gets attributed.
        await asyncio.sleep(0.6)
        rid = "route-attribution"
        envelope = {
            "id": rid,
            "status": 200,
            "headers": [],
            "body": base64.b64encode(b'{"ok":true}').decode("ascii"),
            "timing": {
                "version": 1,
                "request_id": rid,
                "process_epoch_ns": 1,
                "queue_dispatch_ns": 1000,
                "decode_ns": 1000,
                "build_ns": 1000,
                "asgi_ns": 1_000_000_000,
                "response_collect_ns": 1000,
                "response_encode_ns": 1000,
            },
        }
        return extension_backend_loader._RoundtripResult(
            json.dumps(envelope).encode("utf-8"), rid, 1100.0
        )

    async def dispatch(path: str):
        return await extension_backend_loader._invoke_backend(
            spec,
            method="GET",
            path=path,
            body_bytes=b"",
            query_b64="",
            safe_headers=[],
            base_url="http://testserver",
        )

    extension_backend_loader._roundtrip = stub_roundtrip
    try:
        # 1.0s on the 360s route is never an incident, however many times.
        for _ in range(4):
            response = asyncio.run(dispatch("post-turn"))
            if response.status_code != 200:
                raise AssertionError(response.status_code)
        if not _is_enabled("probe.declared-strict"):
            raise AssertionError("in-budget route dispatch quarantined the extension")
        # The same 1.0s on the 0.5s route is an incident every time.
        for _ in range(3):
            asyncio.run(dispatch("ping"))
    finally:
        extension_backend_loader._roundtrip = original_roundtrip
    if not (extension_store.get_extension("probe.declared-strict") or {}).get("pending_health_decision"):
        raise AssertionError("dispatch did not attribute the incident to the strict route")


def test_host_timeouts_ignore_the_declared_budget() -> None:
    _seed_store()
    # A host timeout only fires after the declared budget already elapsed, so it
    # must keep counting against the global floor.
    for index in range(2):
        if extension_store.record_backend_timeout(
            "probe.timeouts", activation_id=_activation("probe.timeouts"), elapsed_seconds=5.0
        ):
            raise AssertionError(f"timeout {index} quarantined early")
    disabled = extension_store.record_backend_timeout(
        "probe.timeouts", activation_id=_activation("probe.timeouts"), elapsed_seconds=5.0
    )
    if disabled != ["probe.timeouts"]:
        raise AssertionError(f"repeated timeouts stopped quarantining: {disabled}")
    decision = (extension_store.get_extension("probe.timeouts") or {}).get("pending_health_decision") or {}
    if decision.get("reason") != "repeated_backend_timeouts":
        raise AssertionError(decision)
    if not _is_enabled("probe.timeouts"):
        raise AssertionError("timeout decision request automatically disabled the extension")


def test_route_timeout_resolution_is_shared_with_the_host() -> None:
    """One source of truth for "how long is this route allowed to take": the
    quarantine resolver must agree with the host's timeout resolver."""
    timeouts = {"a/b": 7.0, "a": 3.0, "default": 2.0}
    spec = {"backend_timeouts": timeouts}
    for path in ("a/b", "a/b/c", "a/x", "z", ""):
        host = extension_backend_loader._resolve_host_timeout(spec, path)
        shared = extension_store.resolve_route_timeout(
            timeouts, path, default_seconds=extension_backend_loader._HOST_TIMEOUT_SECONDS
        )
        if host != shared:
            raise AssertionError(f"route {path!r}: host={host} shared={shared}")
    if extension_store.resolve_route_timeout(None, "x", default_seconds=2.0) != 2.0:
        raise AssertionError("missing declaration must fall back to the given default")
    if extension_store.resolve_route_timeout({"x": 10 ** 9}, "x", default_seconds=2.0) != float(10 ** 9):
        raise AssertionError("declared budget must be honored verbatim")
    # Non-positive budgets never reach either resolver: the manifest is rejected.
    try:
        extension_store._validate_backend_timeouts({"x": -1})
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("non-positive backend_timeouts entry accepted")
    # …and because the host no longer carries its own copy of the lookup rules,
    # even that unreachable shape resolves identically. Two implementations would
    # disagree here.
    rejected_shape = {"x": -1, "default": 2.0}
    if extension_backend_loader._resolve_host_timeout(
        {"backend_timeouts": rejected_shape}, "x"
    ) != extension_store.resolve_route_timeout(
        rejected_shape, "x", default_seconds=extension_backend_loader._HOST_TIMEOUT_SECONDS
    ):
        raise AssertionError("host still resolves route timeouts with its own implementation")


def main() -> None:
    tests = [
        test_declared_route_budget_is_not_quarantined_within_budget,
        test_route_without_declaration_requests_decision_at_global_floor,
        test_exceeding_a_generous_declared_budget_still_quarantines,
        test_strict_route_below_the_global_floor_is_still_judged_by_its_budget,
        test_caller_pre_gate_forwards_the_same_incidents_the_store_counts,
        test_invoke_backend_attributes_the_route_it_dispatched,
        test_host_timeouts_ignore_the_declared_budget,
        test_route_timeout_resolution_is_shared_with_the_host,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports every failure
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
