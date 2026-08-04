from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = Path(_test_home.isolate("ba-test-recovery-selector-authority-"))

from event_bus import EventBus  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from lifecycle_command_model import (  # noqa: E402
    ExecutionTurnIdentity,
    SelectorIdentity,
    UserTurnIdentity,
)
import lifecycle_command_store  # noqa: E402
import session_manager  # noqa: E402
import run_recovery  # noqa: E402
from lifecycle_command_states import LifecycleCommandRejected  # noqa: E402
from run_recovery import _attach_recovered_selector_sid  # noqa: E402
from run_recovery import (  # noqa: E402
    _prepared_retry_matches_selector_attempt,
    _recovered_retry_selector_attempt,
)


def _compatibility(namespace: str) -> dict:
    return {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": str(TEST_HOME / "claude" / "projects"),
        "claude_project_namespace": namespace,
    }


async def _elect(
    engine: LifecycleCommandEngine,
    *,
    session_id: str,
    suffix: str,
    selector: SelectorIdentity,
    compatibility: dict,
) -> tuple[ExecutionTurnIdentity, str]:
    lifecycle_id = f"lifecycle-{suffix}"
    execution = ExecutionTurnIdentity(
        f"execution-{suffix}",
        f"assistant-{suffix}",
        "native",
    )
    provider_run_id = f"provider-run-{suffix}"
    await engine.begin_turn(
        request_id=f"begin-{suffix}",
        session_id=session_id,
        identity=UserTurnIdentity(lifecycle_id, lifecycle_id),
    )
    await engine.start_execution(
        session_id,
        execution_identity=execution,
        selector_role="primary",
    )
    authority, decision = lifecycle_command_store.persist_admitted_selector_attempt(
        session_id,
        target=selector,
        native_sid_compatibility=compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    await engine.elect_execution_attempt(
        session_id,
        execution_identity=execution,
        provider_run_id=provider_run_id,
        selector_generation=authority.generation,
        native_sid_compatibility=compatibility,
        selector=selector,
    )
    return execution, provider_run_id


async def test_recovery_selector_attachment_is_generation_fenced_and_crash_safe(
    monkeypatch,
) -> None:
    session_id = "recovery-selector-authority"
    projected: list[tuple[str, str, str | None]] = []
    fail_projection = False

    monkeypatch.setattr(
        session_manager.manager,
        "get",
        lambda requested_id: (
            {"orchestration_mode": "native"}
            if requested_id == session_id
            else None
        ),
    )

    def set_agent_sid(requested_id, mode, native_sid, **_metadata):
        if fail_projection:
            raise RuntimeError("simulated projection crash")
        projected.append((requested_id, mode, native_sid))
        return {"id": requested_id}

    monkeypatch.setattr(session_manager.manager, "set_agent_sid", set_agent_sid)

    compatibility_a = _compatibility("-repo-a")
    compatibility_c = _compatibility("-repo-c")
    selector_a = SelectorIdentity("provider-a", "model-a", "runner-a")
    selector_b = SelectorIdentity("provider-b", "model-b", "runner-b")
    selector_c = SelectorIdentity("provider-c", "model-c", "runner-c")
    engine = LifecycleCommandEngine(EventBus())

    execution_a, run_a = await _elect(
        engine,
        session_id=session_id,
        suffix="a",
        selector=selector_a,
        compatibility=compatibility_a,
    )
    await engine.finish_execution_and_turn(
        session_id,
        execution_identity=execution_a,
        provider_run_id=run_a,
        outcome="failed",
    )
    authority_b = lifecycle_command_store.persist_selector_transition(
        session_id,
        target=selector_b,
        projection_updates={"provider_id": "provider-b", "model": "model-b", "runner": "runner-b"},
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_legacy_native_sid_compatibility=None,
        supervisor_legacy_native_sid_compatibility=None,
    )
    assert lifecycle_command_store.acknowledge_selector_projection(
        session_id,
        authority_b.identity,
    )
    authority_c = lifecycle_command_store.persist_selector_transition(
        session_id,
        target=selector_c,
        projection_updates={"provider_id": "provider-c", "model": "model-c", "runner": "runner-c"},
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_legacy_native_sid_compatibility=None,
        supervisor_legacy_native_sid_compatibility=None,
    )
    assert lifecycle_command_store.acknowledge_selector_projection(
        session_id,
        authority_c.identity,
    )
    execution_c, run_c = await _elect(
        engine,
        session_id=session_id,
        suffix="c",
        selector=selector_c,
        compatibility=compatibility_c,
    )

    assert not await engine.attach_recovered_selector_native_sid(
        session_id,
        provider_run_id=run_a,
        native_sid="native-a-stale",
        native_sid_compatibility=compatibility_a,
        selector=selector_a,
    )
    assert not await engine.attach_recovered_selector_native_sid(
        session_id,
        provider_run_id=run_c,
        native_sid="native-c-wrong-proof",
        native_sid_compatibility=compatibility_a,
        selector=selector_c,
    )

    fail_projection = True
    with pytest.raises(RuntimeError, match="simulated projection crash"):
        await engine.attach_recovered_selector_native_sid(
            session_id,
            provider_run_id=run_c,
            native_sid="native-c",
            native_sid_compatibility=compatibility_c,
            selector=selector_c,
        )
    pending = lifecycle_command_store.pending_selector_projections()
    assert len(pending) == 1
    await engine.close()

    fail_projection = False
    recovered = LifecycleCommandEngine(EventBus())
    await recovered.bind()
    assert projected == [(session_id, "native", "native-c")]
    assert lifecycle_command_store.pending_selector_projections() == ()
    assert await recovered.attach_recovered_selector_native_sid(
        session_id,
        provider_run_id=run_c,
        native_sid="native-c",
        native_sid_compatibility=compatibility_c,
        selector=selector_c,
    )
    assert projected == [
        (session_id, "native", "native-c"),
        (session_id, "native", "native-c"),
    ]
    await recovered.finish_execution_and_turn(
        session_id,
        execution_identity=execution_c,
        provider_run_id=run_c,
        outcome="failed",
    )
    await recovered.close()


async def test_recovery_descriptor_routes_through_lifecycle_authority(
    monkeypatch,
) -> None:
    session_id = "recovery-selector-descriptor"
    projected: list[str] = []
    monkeypatch.setattr(
        session_manager.manager,
        "get",
        lambda requested_id: (
            {"orchestration_mode": "native"}
            if requested_id == session_id
            else None
        ),
    )
    monkeypatch.setattr(
        session_manager.manager,
        "set_agent_sid",
        lambda _session_id, _mode, native_sid, **_metadata: (
            projected.append(native_sid) or {"id": session_id}
        ),
    )
    compatibility = _compatibility("-descriptor")
    selector = SelectorIdentity(
        "provider-descriptor",
        "model-descriptor",
        "runner-descriptor",
    )
    engine = LifecycleCommandEngine(EventBus())
    execution, provider_run_id = await _elect(
        engine,
        session_id=session_id,
        suffix="descriptor",
        selector=selector,
        compatibility=compatibility,
    )
    coordinator = SimpleNamespace(lifecycle_commands=engine)
    descriptor = {
        "run_id": provider_run_id,
        "app_session_id": session_id,
        "provider_id": selector.provider_id,
        "model": selector.model,
        "runner": selector.runner,
        "native_sid_compatibility": compatibility,
    }
    assert await _attach_recovered_selector_sid(
        coordinator,
        descriptor,
        "native-descriptor",
    )
    assert not await _attach_recovered_selector_sid(
        coordinator,
        {**descriptor, "run_id": "provider-run-stale-descriptor"},
        "native-stale-descriptor",
    )
    assert not await _attach_recovered_selector_sid(
        coordinator,
        {**descriptor, "runner": "runner-tampered"},
        "native-tampered-descriptor",
    )
    assert projected == ["native-descriptor"]
    await engine.finish_execution_and_turn(
        session_id,
        execution_identity=execution,
        provider_run_id=provider_run_id,
        outcome="failed",
    )
    await engine.close()


async def test_election_commit_rejects_authority_race_without_rebinding(
    monkeypatch,
) -> None:
    session_id = "recovery-selector-election-race"
    compatibility = _compatibility("-election-race")
    selector_a = SelectorIdentity("provider-a", "model-a", "runner-a")
    selector_c = SelectorIdentity("provider-c", "model-c", "runner-c")
    engine = LifecycleCommandEngine(EventBus())
    execution, run_a = await _elect(
        engine,
        session_id=session_id,
        suffix="election-race-a",
        selector=selector_a,
        compatibility=compatibility,
    )
    authority, decision = lifecycle_command_store.persist_admitted_selector_attempt(
        session_id,
        target=selector_a,
        native_sid_compatibility=compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    original_commit = lifecycle_command_store.commit_transition
    raced = False

    def commit_after_authority_changes(requested_session_id, request_id):
        nonlocal raced
        if not raced and requested_session_id == session_id:
            raced = True
            lifecycle_command_store.persist_selector_transition(
                session_id,
                target=selector_c,
                projection_updates={
                    "provider_id": "provider-c",
                    "model": "model-c",
                    "runner": "runner-c",
                },
                primary_native_sid=None,
                supervisor_native_sid=None,
                primary_legacy_native_sid_compatibility=None,
                supervisor_legacy_native_sid_compatibility=None,
            )
        return original_commit(requested_session_id, request_id)

    monkeypatch.setattr(
        lifecycle_command_store,
        "commit_transition",
        commit_after_authority_changes,
    )
    with pytest.raises(
        LifecycleCommandRejected,
        match="lost its admitted authority",
    ):
        await engine.elect_execution_attempt(
            session_id,
            execution_identity=execution,
            provider_run_id="provider-run-election-race-b",
            selector_generation=authority.generation,
            native_sid_compatibility=compatibility,
            selector=selector_a,
        )
    current = engine.snapshot(session_id).execution
    assert current is not None
    assert current.provider_run_id == run_a
    assert lifecycle_command_store.execution_selector_attempt(
        session_id,
        "provider-run-election-race-b",
    ) is None
    assert lifecycle_command_store.acknowledge_selector_projection(
        session_id,
        selector_c,
    )
    await engine.close()


async def test_legacy_attempt_backfill_requires_exact_current_evidence() -> None:
    session_id = "recovery-selector-legacy-backfill"
    compatibility = _compatibility("-legacy-backfill")
    selector = SelectorIdentity("provider", "model", "runner")
    engine = LifecycleCommandEngine(EventBus())
    _execution, provider_run_id = await _elect(
        engine,
        session_id=session_id,
        suffix="legacy-backfill",
        selector=selector,
        compatibility=compatibility,
    )
    with lifecycle_command_store.connection() as database:
        database.execute(
            """
            DELETE FROM execution_selector_attempts
            WHERE session_id = ? AND provider_run_id = ?
            """,
            (session_id, provider_run_id),
        )
        database.commit()
    assert await engine.recover_execution_selector_attempt(
        session_id,
        provider_run_id=provider_run_id,
        native_sid_compatibility=_compatibility("-wrong"),
        selector=selector,
    ) is None
    assert lifecycle_command_store.execution_selector_attempt(
        session_id,
        provider_run_id,
    ) is None
    recovered = await engine.recover_execution_selector_attempt(
        session_id,
        provider_run_id=provider_run_id,
        native_sid_compatibility=compatibility,
        selector=selector,
    )
    assert recovered is not None
    assert recovered["selector"] == selector
    assert recovered["native_sid_compatibility"] == compatibility
    await engine.close()


async def test_retry_clones_exact_frozen_attempt_evidence() -> None:
    session_id = "recovery-selector-retry-evidence"
    compatibility = _compatibility("-retry-evidence")
    selector = SelectorIdentity("provider", "model", "runner")
    engine = LifecycleCommandEngine(EventBus())
    _execution, provider_run_id = await _elect(
        engine,
        session_id=session_id,
        suffix="retry-evidence",
        selector=selector,
        compatibility=compatibility,
    )
    descriptor = {
        "run_id": provider_run_id,
        "app_session_id": session_id,
        "provider_id": selector.provider_id,
        "model": selector.model,
        "runner": selector.runner,
        "native_sid_compatibility": compatibility,
    }
    attempt = await _recovered_retry_selector_attempt(
        engine,
        app_session_id=session_id,
        provider_run_id=provider_run_id,
        desc=descriptor,
    )
    assert attempt is not None
    assert attempt["selector"] == selector
    assert attempt["native_sid_compatibility"] == compatibility
    assert await _recovered_retry_selector_attempt(
        engine,
        app_session_id=session_id,
        provider_run_id=provider_run_id,
        desc={**descriptor, "runner": "tampered-runner"},
    ) is None
    prepared = SimpleNamespace(artifact=SimpleNamespace(
        provider_id=selector.provider_id,
        template=SimpleNamespace(arguments=lambda: {"model": selector.model}),
        runtime_policy={"native_sid_compatibility": compatibility},
    ))
    assert _prepared_retry_matches_selector_attempt(prepared, attempt)
    prepared.artifact.runtime_policy = {
        "native_sid_compatibility": _compatibility("-changed")
    }
    assert not _prepared_retry_matches_selector_attempt(prepared, attempt)
    await engine.close()


@pytest.mark.parametrize(
    ("accepted", "expected_sid"),
    ((True, "native-complete-only"), (False, None)),
)
async def test_complete_only_recovered_sid_is_authorized_before_finalization(
    monkeypatch,
    accepted: bool,
    expected_sid: str | None,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(SimpleNamespace(
        type="complete",
        data={"session_id": "native-complete-only"},
    ))
    finalized: list[str | None] = []

    monkeypatch.setattr(
        run_recovery.session_manager,
        "claim_owner",
        lambda _sid: object(),
    )

    async def attach(_coordinator, _desc, native_sid):
        return native_sid if accepted else None

    async def finalize(
        _coordinator,
        _provider,
        _desc,
        _recovering_msg_id,
        *,
        approved_recovered_sid=None,
    ):
        finalized.append(approved_recovered_sid)

    monkeypatch.setattr(run_recovery, "_attach_recovered_selector_sid", attach)
    monkeypatch.setattr(run_recovery, "_finalize_when_done", finalize)
    await run_recovery._drain_recovered_live_queue(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "run_id": "complete-only-run",
            "app_session_id": "complete-only-session",
        },
        queue,
        "assistant-complete-only",
    )
    assert finalized == [expected_sid]
