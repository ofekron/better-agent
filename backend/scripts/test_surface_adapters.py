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
from backend.adapters.store_access import store_access  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.adapter import BetterAgentAdapter  # noqa: E402
from backend.surface_contract.chat_surface import ChatSurface  # noqa: E402
from backend.surface_contract.descriptors import AuthFlow, ConfigState  # noqa: E402
from backend.surface_contract.identity import Ok, Rebuilding  # noqa: E402
from backend.surface_contract.provider_config_surface import ProviderConfigSurface  # noqa: E402
from backend.surface_contract.runs_surface import RunPhase, RunsSurface  # noqa: E402
from backend.surface_contract.session_surface import (
    SessionRemoved,
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
    session = session_store.create_session(
        name=f"surface session {uuid.uuid4().hex}", model="test-model", cwd=cwd,
        reasoning_effort="high", orchestration_mode="native",
    )

    adapter = SessionSurfaceAdapter()
    result = adapter.list_sessions(None, None)
    assert isinstance(result, Ok), result
    match = next((s for s in result.value.sessions if s.session_id == session["id"]), None)
    assert match is not None
    assert match.title == session["name"]
    assert match.selectors.cwd == cwd
    assert match.selectors.model == "test-model"
    assert match.selectors.reasoning_effort == "high"
    assert match.selectors.orchestration_mode == "native"
    assert match.archived is False
    # No session.fire.*/session.status_projected fact has been observed for
    # this sid yet — conservatively IDLE, per _derive_rollup's default.
    assert match.state == SessionRollupState.IDLE
    assert match.attention_markers == ()
    # B1: `cwd` has no matching Project record — honestly None, not every
    # ad-hoc working directory treated as a tracked project.
    assert match.project_ref is None


def test_live_refresh_selectors_include_runtime_profile_id() -> None:
    # `runtime_profile_id` is absent from session_store's list-summary shape
    # (session_store.py's `_build_summary_for_root`) — only the single-
    # session get_session() read carries it. `_refresh` (the live-fact
    # path) uses that single-record read, so it's the one path that can
    # surface it; list_sessions() (the list-summary path) cannot.
    provider = config_store.add_provider({
        "name": f"Selector Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
    })
    profile = config_store.add_runtime_profile({
        "provider_id": provider["id"], "runner": "better_agent_runner",
    })
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(
        name=f"profile session {uuid.uuid4().hex}", cwd=cwd, runtime_profile_id=profile["id"],
    )
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
        assert upserts[0].summary.selectors.runtime_profile_id == profile["id"]
    finally:
        sub.close()


def test_session_fire_running_and_error_changed_update_rollup() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(name=f"fire rollup {uuid.uuid4().hex}", model="m", cwd=cwd)
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()

    asyncio.run(bus.publish(BusEvent(
        type="session.fire.running_changed", root_id=sid, sid=sid,
        payload={"kind": "running_changed", "value": True}, persist=False,
    )))
    result = adapter.list_sessions(None, None)
    assert isinstance(result, Ok), result
    match = next(s for s in result.value.sessions if s.session_id == sid)
    assert match.state == SessionRollupState.RUNNING

    # errored takes priority over running, mirroring session_status.key_for.
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.error_changed", root_id=sid, sid=sid,
        payload={"kind": "error_changed", "has_error": True, "error": "boom"}, persist=False,
    )))
    result2 = adapter.list_sessions(None, None)
    match2 = next(s for s in result2.value.sessions if s.session_id == sid)
    assert match2.state == SessionRollupState.FAILED


def test_session_status_projected_maps_waiting_for_user() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(name=f"status rollup {uuid.uuid4().hex}", model="m", cwd=cwd)
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()

    asyncio.run(bus.publish(BusEvent(
        type="session.status_projected", root_id=sid, sid=sid,
        payload={
            "session_id": sid, "running": False, "unread": False,
            "waiting_for_user": True, "errored": False,
            "status_key": "needs_decision", "status_rank": 5,
        },
        persist=False,
    )))
    result = adapter.list_sessions(None, None)
    assert isinstance(result, Ok), result
    match = next(s for s in result.value.sessions if s.session_id == sid)
    assert match.state == SessionRollupState.AWAITING_APPROVAL


def test_session_deleted_fact_evicts_rollup_cache() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-surface-session-cwd-")
    session = session_store.create_session(name=f"delete rollup {uuid.uuid4().hex}", model="m", cwd=cwd)
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()

    asyncio.run(bus.publish(BusEvent(
        type="session.fire.running_changed", root_id=sid, sid=sid,
        payload={"kind": "running_changed", "value": True}, persist=False,
    )))
    running_result = adapter.list_sessions(None, None)
    running_match = next(s for s in running_result.value.sessions if s.session_id == sid)
    assert running_match.state == SessionRollupState.RUNNING

    # B4: SessionRemoved is an authoritative tombstone — a live subscriber
    # learns of the deletion immediately from the frame itself, not merely
    # from the rollup resetting to IDLE on a subsequent re-list.
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(bus.publish(BusEvent(
            type="session.fire.deleted", root_id=sid, sid=sid,
            payload={"kind": "deleted", "deleted_sids": [sid], "root_id": sid}, persist=False,
        )))
        removed = [f for f in received if isinstance(f, SessionRemoved) and f.session_id == sid]
        assert removed, received
    finally:
        sub.close()
    after_result = adapter.list_sessions(None, None)
    after_match = next(s for s in after_result.value.sessions if s.session_id == sid)
    assert after_match.state == SessionRollupState.IDLE


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


def test_projects_maps_seeded_project_real_session_count() -> None:
    """B1: `session_count` is a real count of sessions whose `cwd` matches
    the project's `path` (`project_store.session_counts_by_cwd`), not a
    hardcoded 0."""
    import project_store  # bare — matches store_access._resolve aliasing

    project_dir = tempfile.mkdtemp(prefix="ba-surface-project-")
    added = project_store.add_project(project_dir, name=f"surface project {uuid.uuid4().hex}")
    assert added is not None

    adapter = SessionSurfaceAdapter()
    empty_result = adapter.projects()
    assert isinstance(empty_result, Ok), empty_result
    empty_match = next((p for p in empty_result.value if p.project_ref == added["path"]), None)
    assert empty_match is not None
    assert empty_match.name == added["name"]
    assert empty_match.session_count == 0

    session_store.create_session(name="in project", model="m", cwd=added["path"])
    session_store.create_session(name="also in project", model="m", cwd=added["path"])
    populated_result = adapter.projects()
    populated_match = next(
        p for p in populated_result.value if p.project_ref == added["path"]
    )
    assert populated_match.session_count == 2


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
    # config_store's `mode` field (subscription|api_key) maps to auth_flows.
    assert match.auth_flows == (AuthFlow.OAUTH_SUBSCRIPTION,)
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


def test_provider_api_key_mode_maps_auth_flow_and_credential_status() -> None:
    provider = config_store.add_provider({
        "name": f"API Key Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "api_key",
    })

    adapter = ProviderConfigSurfaceAdapter()
    match = next(d for d in adapter.list_providers() if d.provider_id == provider["id"])
    assert match.auth_flows == (AuthFlow.API_KEY,)
    # No desktop credential-session connection exists in this test process
    # (see test_store_access.py's equivalent test) — the credential broker
    # status is deterministically "blocked", which maps to CREDENTIAL_FAILED.
    assert match.config_state == ConfigState.CREDENTIAL_FAILED


def test_runtime_profiles_maps_seeded_profile() -> None:
    provider = config_store.add_provider({
        "name": f"Profile Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
    })
    profile = config_store.add_runtime_profile({
        "provider_id": provider["id"], "runner": "better_agent_runner",
    })

    adapter = ProviderConfigSurfaceAdapter()
    # Closure 3 (RuntimeProfile v2 parity): runtime_profiles() now returns
    # the full RuntimeProfilesSnapshot, not a bare tuple — .profiles.
    snapshot = adapter.runtime_profiles()
    match = next(
        (p for p in snapshot.profiles if p.runtime_profile_id == profile["id"]), None,
    )
    assert match is not None
    assert match.provider_id == provider["id"]
    assert match.runner == profile["runner"]
    assert match.deleted_at is None
    assert snapshot.default_runtime_profile_id is not None


def test_installable_catalog_serves_backend_templates() -> None:
    adapter = ProviderConfigSurfaceAdapter()
    catalog = adapter.installable_catalog()
    assert len(catalog) >= 19
    kinds = {d.kind for d in catalog}
    assert "claude" in kinds


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

def _seed_run(
    app_session_id: str,
    *,
    success: bool | None,
    error: str | None = None,
    turn_id: str | None = None,
) -> str:
    root = runs_dir.runs_root()
    run_id = f"run-{uuid.uuid4().hex}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.atomic_write_json(run_dir / "state.json", {
        "session_id": f"prov-{run_id}",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": app_session_id,
    })
    if turn_id is not None:
        # Mirrors `runner.py`'s `turn_dir(run_dir, turn_id)` — the run's
        # owning turn id is the single child directory name under `turns/`.
        (run_dir / "turns" / turn_id).mkdir(parents=True, exist_ok=True)
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
    by_id = {r.run_id: r for r in result.value.runs}
    assert by_id[completed_run].phase == RunPhase.COMPLETED
    assert by_id[failed_run].phase == RunPhase.FAILED
    assert by_id[running_run].phase == RunPhase.STARTING
    # No `turns/<turn_id>` dir seeded for this run — see
    # test_adapter_runs.py for turn_id/heartbeat coverage.
    assert by_id[completed_run].turn_id is None
    # runner is joined via the run's session -> provider -> runtime profile
    # (see test_list_runs_runner_joined_via_session_provider below); no
    # session is seeded for this run's app_session_id, so the join bottoms
    # out at "" here rather than guessed.
    assert by_id[completed_run].runner == ""
    assert by_id[completed_run].last_heartbeat_at is None


def test_list_runs_runner_joined_via_session_provider() -> None:
    provider = config_store.add_provider({
        "name": f"Runner Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "subscription",
    })
    # add_provider seeds its own default runtime profile — the join takes
    # whichever runtime profile store_access surfaces first for this
    # provider (documented, ambiguous-if->1 gap), so the oracle is read the
    # SAME way rather than assuming a specific runner string.
    expected_runner = next(
        p.runner for p in store_access.list_runtime_profiles() if p.provider_id == provider["id"]
    )
    cwd = tempfile.mkdtemp(prefix="ba-surface-runs-cwd-")
    session = session_store.create_session(
        name=f"runner join session {uuid.uuid4().hex}", cwd=cwd, provider_id=provider["id"],
    )
    run_id = _seed_run(session["id"], success=True)

    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session["id"], None)
    assert isinstance(result, Ok), result
    match = next(r for r in result.value.runs if r.run_id == run_id)
    assert match.provider_id == provider["id"]
    assert match.runner == expected_runner
    assert expected_runner


def test_list_runs_filters_by_session_id() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=True)
    _seed_run(f"app-{uuid.uuid4().hex}", success=True)

    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert {r.run_id for r in result.value.runs} == {run_id}


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


def test_usage_analytics_returns_ok_page_seeded_from_llm_call_log() -> None:
    """E6: `usage_analytics()` is real now (`backend/adapters/
    usage_analytics.py`) — see `test_usage_analytics.py` for the full
    aggregation + pagination + honest-absence coverage. This one
    integration check proves the adapter is wired through `store_access.
    list_llm_call_records()`, not the old permanent `Rebuilding` stub."""
    import llm_call_log
    from backend.surface_contract.runs_surface import UsageMeasureState

    provider_id = f"prov-{uuid.uuid4().hex}"
    llm_call_log.append_call(
        source="turn", reason="test", provider_id=provider_id, model="usage-test-model",
        trace_id=f"tr_{uuid.uuid4().hex[:12]}", token_usage={"total_tokens": 9},
    )

    adapter = RunsSurfaceAdapter()
    rows: list = []
    cursor = None
    for _ in range(20):  # bounded walk — other tests may share this process's test home
        result = adapter.usage_analytics(cursor)
        assert isinstance(result, Ok), result
        rows.extend(result.value.rows)
        cursor = result.value.next_cursor
        if cursor is None:
            break
    match = next(r for r in rows if r.provider_id == provider_id)
    assert match.model == "usage-test-model"
    assert match.state == UsageMeasureState.REPORTED
    assert match.tokens == 9


def test_runs_lifecycle_fact_broadcasts_best_effort_run_summary_upsert() -> None:
    from backend.surface_contract.runs_surface import RunSummaryUpsert

    session_id = f"app-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=True, turn_id=turn_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(
            bus.publish(
                BusEvent(
                    type="lifecycle.turn_complete", root_id=session_id, sid=session_id,
                    payload={"reason": "success", "execution_turn_id": turn_id}, persist=False,
                )
            )
        )
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert upserts, received
        assert upserts[0].summary.run_id == run_id
        assert upserts[0].summary.turn_id == turn_id
    finally:
        sub.close()


def test_runs_lifecycle_fact_without_matching_turn_id_does_not_broadcast() -> None:
    """E5: the exact turn_id match must fail closed — no misattributed
    push — when no run directory carries this execution_turn_id yet."""
    from backend.surface_contract.runs_surface import RunSummaryUpsert

    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id, success=True, turn_id=f"turn-{uuid.uuid4().hex}")

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(
            bus.publish(
                BusEvent(
                    type="lifecycle.turn_complete", root_id=session_id, sid=session_id,
                    payload={"reason": "success", "execution_turn_id": f"turn-{uuid.uuid4().hex}"},
                    persist=False,
                )
            )
        )
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert not upserts, received
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
    test_live_refresh_selectors_include_runtime_profile_id,
    test_session_fire_running_and_error_changed_update_rollup,
    test_session_status_projected_maps_waiting_for_user,
    test_session_deleted_fact_evicts_rollup_cache,
    test_list_sessions_query_filters_by_title,
    test_projects_maps_seeded_project_real_session_count,
    test_session_fire_fact_triggers_change_only_upsert,
    test_provider_descriptor_mapping,
    test_provider_suspended_maps_to_suspended_config_state,
    test_provider_api_key_mode_maps_auth_flow_and_credential_status,
    test_runtime_profiles_maps_seeded_profile,
    test_installable_catalog_serves_backend_templates,
    test_provider_submit_rejects_all_intents,
    test_list_runs_phase_mapping_completed_failed_running,
    test_list_runs_runner_joined_via_session_provider,
    test_list_runs_filters_by_session_id,
    test_run_detail_maps_entries_and_missing_is_rebuilding,
    test_usage_analytics_returns_ok_page_seeded_from_llm_call_log,
    test_runs_lifecycle_fact_broadcasts_best_effort_run_summary_upsert,
    test_runs_lifecycle_fact_without_matching_turn_id_does_not_broadcast,
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
