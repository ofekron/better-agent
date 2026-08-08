#!/usr/bin/env python3
"""Unit coverage for backend/adapters/system_adapter.py (ADR 0011 System &
Host Surface, Part 1).

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched), no LLM/provider subprocess involved. Mirrors
`test_surface_adapters.py`'s idiom: seed state through the stores' own
public write APIs (bare-imported), then read it back through the dotted
`backend.adapters.*` surface; for live-plane coverage, publish a `BusEvent`
on the dotted `backend.event_bus.bus` (the SAME bus `BusBoundProjection`
subscribed on) and assert the adapter's `subscribe(emit)` callback saw the
expected frame.

Run:
    PYTHONPATH=.:./backend:./sdk python3 -m pytest backend/scripts/test_adapter_system.py -q
    PYTHONPATH=.:./backend:./sdk python3 backend/scripts/test_adapter_system.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-system-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from backend.adapters.system_adapter import SystemSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.identity import IntentId, Ok  # noqa: E402
from backend.surface_contract.intents import (  # noqa: E402
    DeleteSchedule,
    IntentRejected,
    SetInstallationCapability,
)
from backend.surface_contract.system_surface import (  # noqa: E402
    HostStartupTaskCleared,
    HostStartupTaskState,
    HostStartupTaskUpsert,
    InstallationCapabilityChanged,
    NodeRegistrationDecision,
    NodeRegistrationState,
    NodeRegistrationUpsert,
    ScheduleRemoved,
    ScheduleUpsert,
)


def _publish(event_type: str, payload: dict, *, sid: str = "system") -> None:
    asyncio.run(bus.publish(BusEvent(
        type=event_type, root_id=sid, sid=sid, payload=payload, persist=False,
    )))


# ---------------------------------------------------------------------------
# Read plane: honest-absence / empty-catalog reads against a fresh test home
# ---------------------------------------------------------------------------

def test_extension_catalog_is_empty_on_fresh_home() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.list_extension_catalog(None)
    assert isinstance(result, Ok), result
    assert result.value.entries == ()


def test_installation_capabilities_is_empty_when_profile_not_active() -> None:
    """Honest-absence (ADR 0011's own invariant): a fresh test home has no
    activated installation profile (`installation_profile.capabilities()`
    reports `status: setup_required`, `capabilities: {}`) — the read
    model reflects that as an empty tuple, never a fabricated default
    toggleable set."""
    adapter = SystemSurfaceAdapter()
    result = adapter.list_installation_capabilities()
    assert isinstance(result, Ok), result
    assert result.value == ()


def test_marketplace_bridge_state_defaults_unpaired() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.marketplace_bridge_state()
    assert isinstance(result, Ok), result
    assert result.value.connection_state.value == "unpaired"
    assert result.value.paired_devices == ()
    assert result.value.revocation_pending is False


def test_harness_profiles_always_carries_synthesized_default() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.list_harness_profiles(None)
    assert isinstance(result, Ok), result
    ids = [p.harness_profile_id for p in result.value.profiles]
    assert "default" in ids
    default = next(p for p in result.value.profiles if p.harness_profile_id == "default")
    assert default.is_default is True


def test_list_machines_includes_primary_node() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.list_machines(None)
    assert isinstance(result, Ok), result
    node_ids = {n.node_id for n in result.value.nodes}
    assert "primary" in node_ids


def test_list_schedules_empty_for_unknown_session() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.list_schedules("no-such-session", None)
    assert isinstance(result, Ok), result
    assert result.value.schedules == ()


def test_host_startup_tasks_empty_snapshot_has_zero_epoch() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.host_startup_tasks()
    assert isinstance(result, Ok), result
    assert result.value.tasks == ()
    assert result.value.epoch == 0


def test_node_registrations_empty_on_fresh_home() -> None:
    adapter = SystemSurfaceAdapter()
    result = adapter.list_node_registrations(None)
    assert isinstance(result, Ok), result
    assert result.value.requests == ()


# ---------------------------------------------------------------------------
# Live plane: bus fact -> re-derive -> broadcast, for the concerns whose
# facts are pure Part-1 additions (own dedicated bus fact type, easy to
# drive from a test without a real schedule/task/capability mutation).
# ---------------------------------------------------------------------------

def test_installation_capability_changed_fact_is_a_noop_when_not_found() -> None:
    """On a fresh test home (no active installation profile — see the
    honest-absence read-plane test above), the capability_id named by the
    fact can never be found in `store_access`'s current projection — the
    handler must not crash and must not broadcast a fabricated frame."""
    adapter = SystemSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish("installation.fire.capability_changed", {"capability_id": "mobile"})
        upserts = [f for f in received if isinstance(f, InstallationCapabilityChanged)]
        assert upserts == []
    finally:
        sub.close()


def test_startup_task_cleared_fact_bumps_epoch() -> None:
    adapter = SystemSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish("background_work.fire.changed", {"cleared": True})
        cleared = [f for f in received if isinstance(f, HostStartupTaskCleared)]
        assert cleared, received
        assert cleared[0].epoch == 1
        snapshot = adapter.host_startup_tasks()
        assert isinstance(snapshot, Ok)
        assert snapshot.value.epoch == 1
    finally:
        sub.close()


def test_startup_task_upsert_fact_reads_through_registry() -> None:
    """ADR 0005: startup work reports into the shared background_work
    registry under owner_id `startup_tasks.OWNER` — the adapter's handler
    must narrow `background_work.fire.changed` (which fires for every
    owner sharing that registry) back down to this one before upserting."""
    from background_work import OWNER_CORE, background_work_registry
    from startup_tasks import OWNER as STARTUP_OWNER

    adapter = SystemSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        item_id = background_work_registry.report(
            owner_kind=OWNER_CORE, owner_id=STARTUP_OWNER,
            local_id="smoke_task", label="Smoke Task",
        )
        _publish("background_work.fire.changed", {"item": {"id": item_id}})
        upserts = [f for f in received if isinstance(f, HostStartupTaskUpsert)]
        assert upserts, received
        assert upserts[0].task.id == item_id
        assert upserts[0].task.state == HostStartupTaskState.RUNNING
    finally:
        sub.close()


def test_registration_requested_then_resolved_round_trips_request_fields() -> None:
    adapter = SystemSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish("machines.fire.registration_requested", {
            "node_id": "worker-1", "address": "10.0.0.5", "cwd_roots": ["/repo"],
            "fingerprint": "ab:cd",
        })
        pending = [f for f in received if isinstance(f, NodeRegistrationUpsert)]
        assert pending, received
        assert pending[-1].request.state == NodeRegistrationState.PENDING
        assert pending[-1].request.request.address == "10.0.0.5"

        _publish("machines.fire.registration_resolved", {"node_id": "worker-1", "status": "approved"})
        resolved = [f for f in received if isinstance(f, NodeRegistrationUpsert)]
        assert resolved[-1].request.state == NodeRegistrationState.RESOLVED
        assert resolved[-1].request.response.decision == NodeRegistrationDecision.APPROVED
        # Request fields survive resolution via the adapter's own cache
        # (the legacy resolved fact payload carries only {node_id, status}).
        assert resolved[-1].request.request.address == "10.0.0.5"
    finally:
        sub.close()


def test_schedule_fire_changed_upserts_then_removes() -> None:
    from datetime import datetime, timedelta

    from stores import schedule_store

    adapter = SystemSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        fire_at = (datetime.now() + timedelta(days=1)).isoformat()
        rec = schedule_store.create(
            app_session_id="sched-session", prompt="do the thing", kind="once",
            fire_at=fire_at,
        )
        _publish("schedule.fire.changed", {"app_session_id": "sched-session"})
        upserts = [f for f in received if isinstance(f, ScheduleUpsert)]
        assert upserts, received
        assert upserts[-1].schedule.schedule_id == rec["id"]
        assert upserts[-1].schedule.enabled is True

        schedule_store.delete(rec["id"])
        _publish("schedule.fire.changed", {"app_session_id": "sched-session"})
        removed = [f for f in received if isinstance(f, ScheduleRemoved)]
        assert removed, received
        assert removed[-1].schedule_id == rec["id"]
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# Command plane: unwired port rejects every intent (same "unsupported_
# contract_phase" idiom RunsSurfaceAdapter/ProviderConfigSurfaceAdapter use)
# ---------------------------------------------------------------------------

def test_submit_rejects_when_no_command_port_wired() -> None:
    adapter = SystemSurfaceAdapter()
    intent = SetInstallationCapability(
        cv=0, intent_id=IntentId("intent-1"), session_id=None,
        capability_id="mobile", enabled=True,
    )
    ack = adapter.submit(intent)
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported_contract_phase"


def test_submit_delete_schedule_dispatches_to_wired_port() -> None:
    calls: list[str] = []

    class _FakePort:
        async def delete_schedule(self, schedule_id: str):
            calls.append(schedule_id)
            from backend.adapters.command_port import CommandResult
            return CommandResult(accepted=True)

    adapter = SystemSurfaceAdapter()
    adapter._command_port = _FakePort()
    intent = DeleteSchedule(
        cv=0, intent_id=IntentId("intent-2"), session_id=None, schedule_id="sched-1",
    )

    async def _run():
        return adapter.submit(intent)

    ack = asyncio.run(_run())
    from backend.surface_contract.intents import IntentAccepted
    assert isinstance(ack, IntentAccepted)
    assert ack.intent_id == "intent-2"


def test_machine_node_upsert_and_chat_node_upsert_have_distinct_wire_types() -> None:
    """`system_surface.MachineNodeUpsert` (machine/node topology) and
    `frames.NodeUpsert` (chat content node) must serialize to different
    `type` strings — `adapter_api._frame_type_name` derives the wire type
    purely from the Python class name, so two classes literally both named
    `NodeUpsert` would collide on the wire even though they come from
    different surfaces."""
    import adapter_api  # bare — matches test_adapter_api.py's own import
    from backend.surface_contract.frames import NodeUpsert as ChatNodeUpsert
    from backend.surface_contract.system_surface import MachineNodeUpsert

    chat_frame = object.__new__(ChatNodeUpsert)
    machine_frame = object.__new__(MachineNodeUpsert)

    chat_wire_type = adapter_api._frame_type_name(chat_frame)
    machine_wire_type = adapter_api._frame_type_name(machine_frame)

    assert chat_wire_type == "node_upsert"
    assert machine_wire_type != "node_upsert"
    assert machine_wire_type != chat_wire_type


_TESTS = [
    test_extension_catalog_is_empty_on_fresh_home,
    test_installation_capabilities_is_empty_when_profile_not_active,
    test_marketplace_bridge_state_defaults_unpaired,
    test_harness_profiles_always_carries_synthesized_default,
    test_list_machines_includes_primary_node,
    test_list_schedules_empty_for_unknown_session,
    test_host_startup_tasks_empty_snapshot_has_zero_epoch,
    test_node_registrations_empty_on_fresh_home,
    test_installation_capability_changed_fact_is_a_noop_when_not_found,
    test_startup_task_cleared_fact_bumps_epoch,
    test_startup_task_upsert_fact_reads_through_registry,
    test_registration_requested_then_resolved_round_trips_request_fields,
    test_schedule_fire_changed_upserts_then_removes,
    test_submit_rejects_when_no_command_port_wired,
    test_submit_delete_schedule_dispatches_to_wired_port,
    test_machine_node_upsert_and_chat_node_upsert_have_distinct_wire_types,
]


def _run_standalone() -> None:
    failures = 0
    for test in _TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    if failures:
        print(f"{failures}/{len(_TESTS)} tests failed")
        sys.exit(1)
    print(f"all {len(_TESTS)} tests passed")


if __name__ == "__main__":
    _run_standalone()
