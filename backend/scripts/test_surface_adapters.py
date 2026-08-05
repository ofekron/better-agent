#!/usr/bin/env python3
"""Unit coverage for backend/adapters/{session,provider,runs}_adapter.py
and backend/adapters/__init__.py's `build_adapter()` (ADR 0007-0009).

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched), no LLM/provider subprocess involved. Seeds
state through the stores' own public write APIs (bare-imported, matching
`test_store_access.py`'s idiom), then reads it back through the dotted
`backend.adapters.*` surfaces — mirroring `test_chat_adapter.py`'s
dotted-bus-for-dotted-adapter pattern (bare `event_bus` and dotted
`backend.event_bus` are two distinct singletons in this codebase, so a
live-fact test must publish on the SAME dotted bus the adapter's
`BusBoundProjection` subscribed on).

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_surface_adapters.py -q
    PYTHONPATH=. python3 backend/scripts/test_surface_adapters.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-surface-adapters-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import config_store  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
import runs_dir  # noqa: E402
import session_store  # noqa: E402

from backend.adapters import build_adapter  # noqa: E402
from backend.adapters.provider_adapter import ProviderConfigSurfaceAdapter  # noqa: E402
from backend.adapters.runs_adapter import RunsSurfaceAdapter  # noqa: E402
from backend.adapters.session_adapter import SessionSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.adapter import BetterAgentAdapter  # noqa: E402
from backend.surface_contract.chat_surface import ChatSurface  # noqa: E402
from backend.surface_contract.descriptors import ConfigState  # noqa: E402
from backend.surface_contract.identity import Ok, Rebuilding  # noqa: E402
from backend.surface_contract.provider_config_surface import ProviderConfigSurface  # noqa: E402
from backend.surface_contract.runs_surface import RunPhase, RunsSurface  # noqa: E402
from backend.surface_contract.session_surface import (
    SessionRollupState,
    SessionSummaryUpsert,
    SessionSurface,
)  # noqa: E402


def _publish_session_fire(sid: str) -> None:
    asyncio.run(
        bus.publish(
            BusEvent(
                type="session.fire.test_kind",
                root_id=sid, sid=sid,
                payload={"kind": "test_kind"},
                persist=False,
            )
        )
    )


# ---------------------------------------------------------------------------
# SessionSurfaceAdapter
# ---------------------------------------------------------------------------

def test_list_sessions_maps_seeded_session() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(name=f"surface session {uuid.uuid4().hex}", model="m", cwd=cwd)

    adapter = SessionSurfaceAdapter()
    result = adapter.list_sessions(None, None)
    assert isinstance(result, Ok), result
    match = next((s for s in result.value.sessions if s.session_id == session["id"]), None)
    assert match is not None
    assert match.title == session["name"]
    assert match.selectors.cwd == cwd
    assert match.archived is False
    # gap: no running/failed/queued signal on SessionRecord — always IDLE.
    assert match.state == SessionRollupState.IDLE
    assert match.attention_markers == ()


def test_list_sessions_query_filters_by_title() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    needle = f"needle-{uuid.uuid4().hex}"
    matching = session_store.create_session(name=f"has the {needle} in it", model="m", cwd=cwd)
    session_store.create_session(name="unrelated session", model="m", cwd=cwd)

    adapter = SessionSurfaceAdapter()
    result = adapter.list_sessions(None, needle.upper())
    assert isinstance(result, Ok), result
    ids = {s.session_id for s in result.value.sessions}
    assert matching["id"] in ids
    assert all(needle in s.title.lower() for s in result.value.sessions)


def test_projects_maps_seeded_project_with_gap_defaults() -> None:
    import project_store  # bare — matches store_access._resolve aliasing

    project_dir = tempfile.mkdtemp(prefix="ba-surface-project-")
    added = project_store.add_project(project_dir, name=f"surface project {uuid.uuid4().hex}")
    assert added is not None

    adapter = SessionSurfaceAdapter()
    result = adapter.projects()
    assert isinstance(result, Ok), result
    match = next((p for p in result.value if p.project_ref == added["path"]), None)
    assert match is not None
    assert match.name == added["name"]
    # gap: no session-count/order source on ProjectRecord.
    assert match.session_count == 0


def test_rearranger_state_is_rebuilding() -> None:
    adapter = SessionSurfaceAdapter()
    result = adapter.rearranger_state()
    assert isinstance(result, Rebuilding)


def test_session_fire_fact_triggers_change_only_upsert() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(name=f"fire test {uuid.uuid4().hex}", model="m", cwd=cwd)
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish_session_fire(sid)
        upserts = [
            f for f in received if isinstance(f, SessionSummaryUpsert) and f.summary.session_id == sid
        ]
        assert upserts, received
        assert upserts[0].summary.title == session["name"]

        received.clear()
        _publish_session_fire(sid)  # no underlying record change this time
        upserts_again = [
            f for f in received if isinstance(f, SessionSummaryUpsert) and f.summary.session_id == sid
        ]
        assert not upserts_again, received
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# ProviderConfigSurfaceAdapter
# ---------------------------------------------------------------------------

def test_provider_descriptor_mapping() -> None:
    provider = config_store.add_provider({
        "name": f"Surface Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
        "custom_models": ["model-a", "model-b"],
    })

    adapter = ProviderConfigSurfaceAdapter()
    descriptors = adapter.list_providers()
    match = next((d for d in descriptors if d.provider_id == provider["id"]), None)
    assert match is not None
    assert match.display.label == provider["name"]
    assert match.display.icon_id == "claude"
    assert match.display.config_copy_key == "provider.config_copy.claude"
    assert match.config_state == ConfigState.ACTIVE
    # gap: no auth-flow signal reaches store_access.
    assert match.auth_flows == ()
    assert isinstance(match.capabilities.usage_reporting, bool)
    assert isinstance(match.capabilities.startup_monitoring, bool)

    catalog = adapter.model_catalog(provider["id"])
    assert {m.model for m in catalog.models} == {"model-a", "model-b"}
    assert all(m.reasoning_efforts == () and m.retired is False for m in catalog.models)


def test_provider_suspended_maps_to_suspended_config_state() -> None:
    provider = config_store.add_provider({
        "name": f"Suspended Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
    })
    config_store.update_provider(provider["id"], {"suspended": True})

    adapter = ProviderConfigSurfaceAdapter()
    match = next(d for d in adapter.list_providers() if d.provider_id == provider["id"])
    assert match.config_state == ConfigState.SUSPENDED


def test_runtime_profiles_maps_seeded_profile() -> None:
    provider = config_store.add_provider({
        "name": f"Profile Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
    })
    profile = config_store.add_runtime_profile({
        "provider_id": provider["id"], "runner": "better_agent_runner",
    })

    adapter = ProviderConfigSurfaceAdapter()
    match = next(
        (p for p in adapter.runtime_profiles() if p.runtime_profile_id == profile["id"]), None,
    )
    assert match is not None
    assert match.provider_id == provider["id"]
    assert match.runner == profile["runner"]


def test_installable_catalog_is_empty() -> None:
    adapter = ProviderConfigSurfaceAdapter()
    assert adapter.installable_catalog() == ()


def test_provider_submit_rejects_all_intents() -> None:
    from backend.surface_contract.intents import BeginLogin

    adapter = ProviderConfigSurfaceAdapter()
    intent = BeginLogin(cv=1, intent_id="intent-1", session_id=None, provider_id="p1", flow="oauth")
    ack = adapter.submit(intent)
    assert ack.intent_id == "intent-1"
    assert ack.code == "unsupported_contract_phase"


# ---------------------------------------------------------------------------
# RunsSurfaceAdapter
# ---------------------------------------------------------------------------

def _seed_run(app_session_id: str, *, success: bool | None, error: str | None = None) -> str:
    root = runs_dir.runs_root()
    run_id = f"run-{uuid.uuid4().hex}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.atomic_write_json(run_dir / "state.json", {
        "session_id": f"prov-{run_id}",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": app_session_id,
    })
    if success is not None:
        runs_dir.atomic_write_json(run_dir / "complete.json", {"success": success, "error": error})
    return run_id


def test_list_runs_phase_mapping_completed_failed_running() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    completed_run = _seed_run(session_id, success=True)
    failed_run = _seed_run(f"app-{uuid.uuid4().hex}", success=False, error="boom")
    running_run = _seed_run(f"app-{uuid.uuid4().hex}", success=None)

    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(None, None)
    assert isinstance(result, Ok), result
    by_id = {r.run_id: r for r in result.value}
    assert by_id[completed_run].phase == RunPhase.COMPLETED
    assert by_id[failed_run].phase == RunPhase.FAILED
    assert by_id[running_run].phase == RunPhase.RUNNING
    # gap: no turn_id / runner / heartbeat source anywhere in store_access.
    assert by_id[completed_run].turn_id is None
    assert by_id[completed_run].runner == ""
    assert by_id[completed_run].last_heartbeat_at is None


def test_list_runs_filters_by_session_id() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=True)
    _seed_run(f"app-{uuid.uuid4().hex}", success=True)

    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert {r.run_id for r in result.value} == {run_id}


def test_run_detail_maps_entries_and_missing_is_rebuilding() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=False, error="boom")

    adapter = RunsSurfaceAdapter()
    result = adapter.run_detail(run_id)
    assert isinstance(result, Ok), result
    keys = {e.key for e in result.value.entries}
    assert "run.detail.run_id" in keys
    assert "run.detail.error" in keys
    assert result.value.summary.phase == RunPhase.FAILED

    missing = adapter.run_detail(f"missing-{uuid.uuid4().hex}")
    assert isinstance(missing, Rebuilding)


def test_usage_analytics_is_rebuilding() -> None:
    adapter = RunsSurfaceAdapter()
    result = adapter.usage_analytics(None)
    assert isinstance(result, Rebuilding)


def test_runs_lifecycle_fact_broadcasts_best_effort_run_summary_upsert() -> None:
    from backend.surface_contract.runs_surface import RunSummaryUpsert

    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=True)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(
            bus.publish(
                BusEvent(
                    type="lifecycle.turn_complete", root_id=session_id, sid=session_id,
                    payload={"reason": "success"}, persist=False,
                )
            )
        )
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert upserts, received
        assert upserts[0].summary.run_id == run_id
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# Composition: build_adapter()
# ---------------------------------------------------------------------------

def test_build_adapter_returns_typed_composition() -> None:
    adapter = build_adapter()
    assert isinstance(adapter, BetterAgentAdapter)
    assert isinstance(adapter.chat, ChatSurface)
    assert isinstance(adapter.sessions, SessionSurface)
    assert isinstance(adapter.providers, ProviderConfigSurface)
    assert isinstance(adapter.runs, RunsSurface)


_TESTS = [
    test_list_sessions_maps_seeded_session,
    test_list_sessions_query_filters_by_title,
    test_projects_maps_seeded_project_with_gap_defaults,
    test_rearranger_state_is_rebuilding,
    test_session_fire_fact_triggers_change_only_upsert,
    test_provider_descriptor_mapping,
    test_provider_suspended_maps_to_suspended_config_state,
    test_runtime_profiles_maps_seeded_profile,
    test_installable_catalog_is_empty,
    test_provider_submit_rejects_all_intents,
    test_list_runs_phase_mapping_completed_failed_running,
    test_list_runs_filters_by_session_id,
    test_run_detail_maps_entries_and_missing_is_rebuilding,
    test_usage_analytics_is_rebuilding,
    test_runs_lifecycle_fact_broadcasts_best_effort_run_summary_upsert,
    test_build_adapter_returns_typed_composition,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
