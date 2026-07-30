"""Startup run recovery, queued-prompt re-enqueue, and post-startup housekeeping.

Live recovered runs are integrated inline by `_recover_in_flight_task` while the
startup gate is held; completed/stale ("cold") runs drain through a bounded
single-worker background queue so they converge without competing with live
reattach. The coordinator and session-tree deletion are injected by the
composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import extension_api
import extension_store
import installation_profile
import native_index_manager
import perf
import user_prefs
from provider import (
    cancel_provider_runs,
    default_provider,
    recover_all_in_flight,
    take_recovery_scan_ownership,
)
from run_recovery import (
    integrate_recovered_runs,
    mark_recovered_runs_terminal,
    pre_provider_orphan_candidates,
    reconcile_missing_bound_lifecycle_orphans,
    reconcile_pre_provider_orphans,
    reconcile_unbound_lifecycle_orphans,
)
from session_helpers import (
    existing_session_ids_async as _existing_session_ids_async,
    session_lite_by_id_async as _session_lite_by_id_async,
)
from session_manager import manager as session_manager
from user_msg_lifecycle import new_lifecycle_msg_id

logger = logging.getLogger(__name__)

_coordinator_ref: Any = None
_delete_session_tree: Callable[[str], Awaitable[bool]] | None = None


def configure(
    *,
    coordinator: Any,
    delete_session_tree: Callable[[str], Awaitable[bool]],
) -> None:
    """Bind the collaborators recovery and housekeeping need."""
    global _coordinator_ref, _delete_session_tree
    _coordinator_ref = coordinator
    _delete_session_tree = delete_session_tree


def _parse_session_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


async def _auto_delete_expired_sessions() -> None:
    days = await asyncio.to_thread(user_prefs.get_session_auto_delete_days)
    if days is None:
        return
    cutoff = datetime.now() - timedelta(days=days)
    summaries = await asyncio.to_thread(session_manager.list)
    for summary in list(summaries):
        sid = summary.get("id")
        if not sid:
            continue
        updated_at = _parse_session_timestamp(summary.get("updated_at"))
        if updated_at is None or updated_at >= cutoff:
            continue
        if _coordinator_ref.turn_manager.is_running_cached(sid):
            continue
        try:
            deleted = await _delete_session_tree(sid)
            if deleted:
                logger.info(
                    "auto_delete_expired_session sid=%s days=%s updated_at=%s",
                    sid, days, summary.get("updated_at"),
                )
        except Exception:
            logger.exception("auto-delete expired session failed sid=%s", sid)


async def _reconcile_queued_delivery_attempt(
    session_id: str,
    queued_id: str,
    lifecycle_msg_id: str,
    queued_attempt: int,
) -> int:
    import lifecycle_command_store

    request_id = f"user-turn:{lifecycle_msg_id}:{queued_attempt}:begin"
    transition = await asyncio.to_thread(
        lifecycle_command_store.transition_for,
        session_id,
        request_id,
    )
    snapshot = _coordinator_ref.lifecycle_commands.snapshot(session_id)
    active_identity = snapshot.identity
    active_same_turn = (
        active_identity is not None
        and active_identity.lifecycle_message_id == lifecycle_msg_id
    )
    if transition is None and active_same_turn:
        execution = snapshot.execution
        if execution is not None and execution.phase not in {
            "complete", "stopped", "failed", "aborted",
        }:
            raise RuntimeError(
                "queued retry collides with an active recovered execution"
            )
        await _coordinator_ref.lifecycle_commands.finish_turn(
            request_id=(
                f"startup-reconcile:{lifecycle_msg_id}:{queued_attempt}:finish"
            ),
            session_id=session_id,
            identity=active_identity,
            outcome="failed",
        )
        return queued_attempt
    if transition is None or active_same_turn:
        return queued_attempt
    if snapshot.identity is not None:
        return queued_attempt
    reserved = await asyncio.to_thread(
        session_manager.reserve_queued_prompt_delivery_attempt,
        session_id,
        queued_id,
    )
    if reserved is None:
        raise RuntimeError(
            "queued prompt disappeared during startup attempt reconciliation"
        )
    return reserved


async def _re_enqueue_queued_prompts() -> set[str]:
    """Re-enqueue accepted prompts that have not become user messages.

    Runs once at startup as crash recovery for the durable prompt outbox.
    Runtime admission uses a coordinator-independent handoff task, so normal
    operation never scans the global session projection."""
    import session_queue_projection
    import team_messaging
    storage_identity = session_manager._root_repository.storage_identity()

    with perf.timed("startup.recovery.projection"):
        rebuilt = await asyncio.to_thread(
            session_queue_projection.ensure_current_or_rebuild,
            storage_identity=storage_identity,
        )
        await asyncio.to_thread(session_manager.rebuild_queued_prompt_counts)
    logger.info(
        "re-enqueue: queue projection %s; scanning projected queued records",
        "rebuilt" if rebuilt else "current",
    )

    re_enqueue_started = time.perf_counter()
    rehydrated_session_ids: set[str] = set()
    queued_records = await asyncio.to_thread(
        session_queue_projection.list_queued_records,
        storage_identity=storage_identity,
    )
    for session in queued_records:
        sid = session.get("id")
        if not sid:
            continue
        # Definitive resurrection guard: never re-submit for a session whose
        # root is gone or tombstoned, regardless of how the stale projection
        # row survived. Drop the row and move on — submit_prompt_async would
        # otherwise materialize a brand-new root for the dead sid.
        if not await asyncio.to_thread(session_manager.is_live_session, sid):
            await asyncio.to_thread(
                session_queue_projection.delete_records,
                [sid],
                storage_identity=storage_identity,
            )
            logger.info(
                "re-enqueue: dropping stale queued record for %s "
                "(no live root / tombstoned)", sid,
            )
            continue
        try:
            queued = session.get("queued_prompts", [])
            if not queued:
                continue

            existing_client_ids = set(
                (session.get("user_message_acks") or {}).keys()
            )
            existing_lifecycle_ids = set(session.get("user_lifecycle_msg_ids") or [])

            for qp in list(queued):
                qp_id = qp.get("id")
                client_id = qp.get("client_id")
                lifecycle_msg_id = qp.get("lifecycle_msg_id")
                if not lifecycle_msg_id:
                    lifecycle_msg_id = new_lifecycle_msg_id()
                    await asyncio.to_thread(
                        session_manager.update_queued_prompt,
                        sid,
                        qp_id,
                        {"lifecycle_msg_id": lifecycle_msg_id},
                    )
                delivery_attempt = await _reconcile_queued_delivery_attempt(
                    sid,
                    qp_id,
                    lifecycle_msg_id,
                    int(qp.get("delivery_attempt") or 0),
                )

                if (
                    (client_id and client_id in existing_client_ids)
                    or (lifecycle_msg_id and lifecycle_msg_id in existing_lifecycle_ids)
                ):
                    await asyncio.to_thread(session_manager.remove_queued_prompt, sid, qp_id)
                    logger.info(
                        "re-enqueue: skipping already-processed queued "
                        "prompt %s for session %s",
                        qp_id, sid,
                    )
                    continue

                team_message = team_messaging.team_message_from_queue_payload(
                    qp,
                    target_session_id=sid,
                )
                params = {
                    "prompt": qp.get("content", ""),
                    "app_session_id": sid,
                    "model": session.get("model"),
                    "cwd": session.get("cwd"),
                    "ws_callback": None,
                    "images": qp.get("images"),
                    "files": qp.get("files"),
                    "orchestration_mode": qp.get("orchestration_mode"),
                    "send_target": qp.get("send_target"),
                    "client_id": client_id,
                    "lifecycle_msg_id": lifecycle_msg_id,
                    "cli_prompt": qp.get("cli_prompt"),
                    "source": qp.get("source"),
                    "team_message": team_message,
                    "disallowed_tools": qp.get("disallowed_tools"),
                    "disabled_builtin_extensions": qp.get("disabled_builtin_extensions"),
                    "capability_contexts": qp.get("capability_contexts") or [],
                    "harness_profile_id": qp.get("harness_profile_id") or "",
                    "_delivery_attempt": delivery_attempt,
                    "_alter_rewind_latest": bool(qp.get("alter_rewind_latest")),
                    "collapse_key": qp.get("collapse_key") or "",
                    "collapse_policy": qp.get("collapse_policy") or "",
                    "_queued_id": qp_id,
                }
                item_id = await _coordinator_ref.submit_prompt_async(
                    sid,
                    params,
                    start_processor=False,
                )
                rehydrated_session_ids.add(sid)
                logger.info(
                    "re-enqueue: re-submitted queued prompt %s -> %s "
                    "for session %s",
                    qp_id, item_id, sid,
                )
        except Exception:
            logger.exception(
                "re-enqueue: failed for session %s, skipping", sid,
            )
    perf.record(
        "startup.recovery.re_enqueue",
        (time.perf_counter() - re_enqueue_started) * 1000.0,
    )
    return rehydrated_session_ids


async def _reconcile_missing_session_runs(cold: list[dict]) -> list[dict]:
    candidate_sids = {
        sid
        for descriptor in cold
        if (sid := _recovered_run_session_id(descriptor))
    }
    existing_sids = await _existing_session_ids_async(candidate_sids)
    missing_terminal = [
        descriptor
        for descriptor in cold
        if (
            (sid := _recovered_run_session_id(descriptor))
            and sid not in existing_sids
            and (
                bool(descriptor.get("has_complete_json"))
                or bool(descriptor.get("cancelled"))
                or bool(descriptor.get("turn_cancelled"))
            )
        )
    ]
    if not missing_terminal:
        return cold
    missing_ids = {id(descriptor) for descriptor in missing_terminal}
    marked = await mark_recovered_runs_terminal(
        missing_terminal,
        "missing session",
    )
    logger.info(
        "recover_all_in_flight: bulk-reconciled %d/%d terminal run(s) "
        "for deleted sessions",
        marked,
        len(missing_terminal),
    )
    return [
        descriptor
        for descriptor in cold
        if id(descriptor) not in missing_ids
    ]


async def _recover_in_flight_task() -> None:
    """Composite body for the `recover_in_flight` startup task: scan
    run dirs on a worker thread (sync FS I/O), then integrate the
    descriptors asynchronously. The startup gate opens after scan and
    classification; replay/finalization is reactive background work and
    must not block normal prompt start."""
    import startup_recovery_gate
    gate_open = False
    startup_tasks: list[asyncio.Task] = []
    try:
        loop = asyncio.get_running_loop()
        candidate_task = asyncio.create_task(
            asyncio.to_thread(pre_provider_orphan_candidates),
            name="startup-pre-provider-orphan-candidates",
        )
        recovery_task = asyncio.create_task(
            _scan_recovered_runs(
                loop,
                candidate_targets=None,
                live_only=True,
            ),
            name="startup-provider-run-classification",
        )
        startup_tasks.extend((candidate_task, recovery_task))
        recovered = await recovery_task
        if recovered and not installation_profile.integrations_enabled():
            import extension_session_ownership
            allowed: list[dict] = []
            recovered_sids = {
                sid
                for descriptor in recovered
                if (sid := str(descriptor.get("app_session_id") or ""))
            }
            lite_by_sid = await _session_lite_by_id_async(recovered_sids)
            for descriptor in recovered:
                session_id = str(descriptor.get("app_session_id") or "")
                session = lite_by_sid.get(session_id) if session_id else None
                native_user_session = bool(
                    session
                    and session.get("orchestration_mode") == "native"
                    and session.get("supervisor_enabled") is not True
                    and not session.get("parent_session_id")
                    and not extension_session_ownership.owner(session_id)
                )
                if native_user_session:
                    allowed.append(descriptor)
                    continue
                provider_id = str(descriptor.get("provider_id") or "")
                run_id = str(descriptor.get("run_id") or "")
                if provider_id and run_id:
                    await asyncio.to_thread(
                        cancel_provider_runs,
                        provider_id,
                        run_ids=[run_id],
                    )
            recovered = allowed
        live_session_ids = _recovered_run_session_ids(
            [descriptor for descriptor in recovered if bool(descriptor.get("alive"))]
        )
        if live_session_ids:
            startup_recovery_gate.register_session_recovery(live_session_ids)
        candidates = await candidate_task
        candidate_targets = {
            (session_id, assistant_id)
            for _, session_id, assistant_id in candidates
        }
        perf.record_count("startup.recovery.candidate_targets", len(candidate_targets))
        ownership_documents, ownership_safe = take_recovery_scan_ownership()
        await reconcile_unbound_lifecycle_orphans(
            _coordinator_ref,
            recovered,
            ownership_documents=ownership_documents,
            ownership_safe=ownership_safe,
            candidates=candidates,
        )
        await reconcile_missing_bound_lifecycle_orphans(
            _coordinator_ref,
            recovered,
            ownership_documents=ownership_documents,
            ownership_safe=ownership_safe,
        )
        await reconcile_pre_provider_orphans(
            _coordinator_ref,
            recovered,
            ownership_documents=ownership_documents,
            ownership_safe=ownership_safe,
            candidates=candidates,
        )
        startup_recovery_gate.mark_recovery_done()
        gate_open = True
        if recovered:
            logger.info("recover_all_in_flight: %d run(s) recovered", len(recovered))
            live = [r for r in recovered if bool(r.get("alive"))]
            cold = [r for r in recovered if not bool(r.get("alive"))]
            cold = await _reconcile_missing_session_runs(cold)
            if live:
                live = _sort_recovered_runs_by_session_priority(live)
                logger.info("recover_all_in_flight: integrating %d live run(s)", len(live))
                with perf.timed("startup.recovery.integration"):
                    remaining_live = list(live)
                    while remaining_live:
                        batch = _pop_next_recovered_session_batch(remaining_live)
                        try:
                            await integrate_recovered_runs(_coordinator_ref, batch)
                        finally:
                            for sid in _recovered_run_session_ids(batch):
                                startup_recovery_gate.mark_session_recovery_done(sid)
        # The gate protects provider-run ownership: classification and every
        # alive run must be registered before rehydrated prompts can start.
        await _coordinator_ref.turn_manager.reconcile_lifecycle_projection()
        rehydrated_session_ids = await _re_enqueue_queued_prompts()
        for sid in sorted(rehydrated_session_ids):
            await _coordinator_ref.start_session_processor_async(sid)
        if recovered:
            if cold:
                _enqueue_recovered_cold_runs(cold)
        background_recovered = await _scan_recovered_runs(
            loop,
            candidate_targets=None,
            exclude_live=True,
        )
        background_cold = [
            descriptor
            for descriptor in background_recovered
            if not bool(descriptor.get("alive"))
        ]
        if background_cold:
            background_cold = await _reconcile_missing_session_runs(
                background_cold,
            )
            if background_cold:
                _enqueue_recovered_cold_runs(background_cold)
        # Resume a native-session import that a restart interrupted.
        await native_index_manager.manager.resume_interrupted_import()
    except asyncio.CancelledError:
        startup_recovery_gate.mark_recovery_failed("recovery cancelled")
        perf.record_count("startup.recovery.cancelled", 1)
        raise
    except Exception as e:
        startup_recovery_gate.mark_recovery_failed(str(e))
        if gate_open:
            logger.exception("recover_all_in_flight: background integration failed")
        raise
    finally:
        unfinished = [task for task in startup_tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)


_RECOVERED_COLD_RUN_WORKER_TASK: Optional[asyncio.Task] = None
_RECOVERED_COLD_PENDING: dict[str, list[dict]] = {}
_RECOVERED_COLD_ACTIVE: set[str] = set()
_RECOVERED_COLD_READY = asyncio.Event()
_RECOVERED_COLD_LOCK = asyncio.Lock()


def _cold_recovery_integration_pending() -> bool:
    """True while `_recovered_cold_run_worker` has pending or in-flight
    batches. This background task integrates completed/stale recovered
    runs outside `turn_manager`'s tracked run state, so restart-cadence
    busy probes must check it explicitly — otherwise a multi-minute cold
    batch (e.g. a large post-restart recovery backlog) reads as idle for
    its entire duration, which can trigger a busy->idle auto-restart mid
    integration and repeat the cycle on the next boot."""
    return bool(_RECOVERED_COLD_PENDING) or bool(_RECOVERED_COLD_ACTIVE)


def _recovered_run_session_id(desc: dict) -> str:
    return str(desc.get("persist_to") or desc.get("app_session_id") or "")


def _recovered_run_session_ids(recovered: list[dict]) -> set[str]:
    return {sid for sid in (_recovered_run_session_id(desc) for desc in recovered) if sid}


def _sort_recovered_runs_by_session_priority(recovered: list[dict]) -> list[dict]:
    import recovery_schedule
    queued_count_by_sid: dict[str, int] = {}

    def queued_priority_rank(desc: dict) -> int:
        sid = _recovered_run_session_id(desc)
        if not sid:
            return 1
        if sid not in queued_count_by_sid:
            queued_count_by_sid[sid] = session_manager.queued_prompt_count(sid)
        return 0 if queued_count_by_sid[sid] > 0 else 1

    return sorted(
        recovered,
        key=lambda desc: (
            recovery_schedule.priority_rank(
                _recovered_run_session_id(desc),
            ),
            queued_priority_rank(desc),
            str(desc.get("run_id") or ""),
        ),
    )


def _pop_next_recovered_session_batch(recovered: list[dict]) -> list[dict]:
    ordered = _sort_recovered_runs_by_session_priority(recovered)
    next_sid = _recovered_run_session_id(ordered[0])
    batch = [desc for desc in recovered if _recovered_run_session_id(desc) == next_sid]
    recovered[:] = [desc for desc in recovered if _recovered_run_session_id(desc) != next_sid]
    return batch


def _recovered_run_session_groups(recovered: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for desc in recovered:
        sid = _recovered_run_session_id(desc)
        if sid:
            groups.setdefault(sid, []).append(desc)
    return groups


def _enqueue_recovered_cold_runs(recovered: list[dict]) -> None:
    """Queue completed/stale recovered runs for low-priority integration.

    Live recovered runs are integrated first in `_recover_in_flight_task`.
    Cold runs no longer wait a fixed 120 seconds; they enter a bounded
    single-worker background queue immediately, in small batches, so stale
    completed output converges quickly without competing with live reattach.
    """
    if not recovered:
        return
    for sid, batch in _recovered_run_session_groups(recovered).items():
        _RECOVERED_COLD_PENDING.setdefault(sid, []).extend(batch)
    _RECOVERED_COLD_READY.set()
    _ensure_recovered_cold_run_worker()
    logger.info(
        "recover_all_in_flight: queued %d completed/stale run(s) for "
        "low-priority integration",
        len(recovered),
    )


def _ensure_recovered_cold_run_worker() -> None:
    global _RECOVERED_COLD_RUN_WORKER_TASK
    if (
        _RECOVERED_COLD_RUN_WORKER_TASK is not None
        and not _RECOVERED_COLD_RUN_WORKER_TASK.done()
    ):
        return
    _RECOVERED_COLD_RUN_WORKER_TASK = asyncio.create_task(
        _recovered_cold_run_worker(),
        name="startup-recover-cold-runs",
    )


async def _recovered_cold_run_worker() -> None:
    while True:
        await _RECOVERED_COLD_READY.wait()
        async with _RECOVERED_COLD_LOCK:
            batch = _pop_next_recovered_cold_batch_locked()
        if not batch:
            continue
        try:
            # Low priority: yield once before each batch so live recovery,
            # re-enqueue, WS, and REST work scheduled by startup can run first.
            import recovery_priority
            await recovery_priority.admit_recovery_quantum()
            started = time.monotonic()
            await integrate_recovered_runs(_coordinator_ref, batch)
            logger.info(
                "recover_all_in_flight: integrated cold batch of %d run(s) "
                "in %.3fs",
                len(batch),
                time.monotonic() - started,
            )
        except Exception:
            logger.exception("recovered cold-run integration failed")
        finally:
            async with _RECOVERED_COLD_LOCK:
                for sid in _recovered_run_session_ids(batch):
                    _RECOVERED_COLD_ACTIVE.discard(sid)


def _pop_next_recovered_cold_batch_locked() -> list[dict]:
    available = [
        desc
        for sid, batch in _RECOVERED_COLD_PENDING.items()
        if sid not in _RECOVERED_COLD_ACTIVE
        for desc in batch
    ]
    if not available:
        if not _RECOVERED_COLD_PENDING:
            _RECOVERED_COLD_READY.clear()
        return []
    ordered = _sort_recovered_runs_by_session_priority(available)
    next_sid = _recovered_run_session_id(ordered[0])
    batch = _RECOVERED_COLD_PENDING.pop(next_sid, [])
    if batch:
        _RECOVERED_COLD_ACTIVE.add(next_sid)
    if not _RECOVERED_COLD_PENDING:
        _RECOVERED_COLD_READY.clear()
    return batch


async def _promote_recovered_session(app_session_id: str) -> None:
    import startup_recovery_gate
    startup_recovery_gate.request_session_priority(app_session_id)
    async with _RECOVERED_COLD_LOCK:
        if app_session_id in _RECOVERED_COLD_ACTIVE:
            return
        batch = _RECOVERED_COLD_PENDING.pop(app_session_id, [])
        if batch:
            _RECOVERED_COLD_ACTIVE.add(app_session_id)
        if not _RECOVERED_COLD_PENDING:
            _RECOVERED_COLD_READY.clear()
    if not batch:
        return
    try:
        started = time.monotonic()
        await integrate_recovered_runs(_coordinator_ref, batch)
        logger.info(
            "recover_all_in_flight: priority-integrated selected session %s "
            "with %d run(s) in %.3fs",
            app_session_id[:8],
            len(batch),
            time.monotonic() - started,
        )
    except Exception:
        logger.exception("priority recovered session integration failed for %s", app_session_id)
    finally:
        async with _RECOVERED_COLD_LOCK:
            _RECOVERED_COLD_ACTIVE.discard(app_session_id)


async def _scan_recovered_runs(loop, **kwargs) -> list[dict]:
    """Run the provider scan/classify phase on the recovery thread.

    The scan is pure file IO, pid checks and JSON parsing: every
    provider's `recover_in_flight` ignores the loop it is handed, and
    nothing in the phase touches the coordinator. That makes it the one
    part of recovery that can leave the main loop wholesale, which
    matters because it is also the long pole at startup.

    Only the SCAN moves. Integration stays on the main loop — it reaches
    loop-bound state (the per-session prompt queue, `turn_manager`'s
    cancel events, the reattach queue handed to the runner) that cannot
    be awaited from another loop.

    `loop` is still the MAIN loop and is passed through unchanged, so a
    provider that starts using it gets the loop the rest of the backend
    lives on rather than recovery's private one.
    """
    import recovery_manager

    def factory():
        return _to_thread_join_on_cancel(
            recover_all_in_flight, loop, **kwargs,
        )

    return await recovery_manager.manager.run(factory)


async def _to_thread_join_on_cancel(fn, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            await worker
        raise


async def _run_maintenance_phase(name: str, fn, *args, **kwargs):
    started = time.perf_counter()
    try:
        result = await _to_thread_join_on_cancel(fn, *args, **kwargs)
    except asyncio.CancelledError:
        perf.record_count(f"startup.maintenance.{name}.cancelled", 1)
        raise
    except Exception:
        perf.record_count(f"startup.maintenance.{name}.error", 1)
        logger.exception("startup maintenance phase %s failed", name)
        return None
    else:
        perf.record_count(f"startup.maintenance.{name}.success", 1)
        return result
    finally:
        perf.record(
            f"startup.maintenance.{name}",
            (time.perf_counter() - started) * 1000.0,
        )


async def _housekeeping_task() -> None:
    """Run non-critical maintenance after startup recovery owns run state."""
    # 1. Prune old run directories only after recovery releases the catalog.
    try:
        ap = default_provider()
        await _run_maintenance_phase("prune_runs", ap.prune_old_runs)
    except Exception:
        logger.exception("housekeeping: prune_old_runs failed")

    # 3. Prune old pending approvals.
    try:
        from stores import pending_approvals
        n = await _run_maintenance_phase("prune_approvals", pending_approvals.prune_old)
        if n:
            logger.info("housekeeping: pruned %d old approval records", n)
    except Exception:
        logger.exception("housekeeping: pending_approvals.prune_old failed")

    # 3b. Prune old pending node-registration requests.
    try:
        from stores import pending_node_registrations
        n = await _run_maintenance_phase(
            "prune_node_registrations", pending_node_registrations.prune_old,
        )
        if n:
            logger.info("housekeeping: pruned %d old node-registration records", n)
    except Exception:
        logger.exception("housekeeping: pending_node_registrations.prune_old failed")

    if not installation_profile.integrations_enabled():
        return

    # 4. Best-effort extension auto-update for refreshable install sources.
    try:
        result = await _run_maintenance_phase(
            "extension_update", extension_store.update_installed_extensions,
        ) or {}
        if result.get("updated"):
            logger.info(
                "housekeeping: auto-updated %d extension(s)",
                result["updated"],
            )
            await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
            import node_config_sync
            node_config_sync.notify_changed("extensions")
    except Exception:
        logger.exception("housekeeping: update_installed_extensions failed")

    # 5. Reapply extension applied-config state on startup. Instruction content
    #    is resolved per session/turn through temporal harness profiles, so there
    #    are no on-disk instruction blocks to sweep.
    try:
        await _run_maintenance_phase(
            "extension_instructions", extension_store.reconcile_all_instructions,
        )
    except Exception:
        logger.exception("housekeeping: reconcile_all_instructions failed")

    # 6. Self-heal extension runtime skills: install enabled extension skills
    #    into ~/.agents/skills and remove disabled/uninstalled extension-owned copies.
    try:
        changed = await _run_maintenance_phase(
            "extension_skills", extension_store.reconcile_runtime_skills,
        )
        if changed:
            logger.info("housekeeping: reconciled %d extension runtime skill item(s)", changed)
    except Exception:
        logger.exception("housekeeping: reconcile_runtime_skills failed")

    # 7. Pre-mint per-extension internal-loopback tokens so out-of-process
    #    native MCP launchers never race to create one.
    try:
        await _run_maintenance_phase(
            "extension_tokens", extension_store.reconcile_extension_tokens,
        )
    except Exception:
        logger.exception("housekeeping: reconcile_extension_tokens failed")

    # 9. Grandfather consent for extensions enabled before the consent feature.
    try:
        grandfathered = await _run_maintenance_phase(
            "extension_consent", extension_store.reconcile_extension_consent,
        )
        if grandfathered:
            logger.info("housekeeping: grandfathered consent for %d extension(s)", grandfathered)
    except Exception:
        logger.exception("housekeeping: reconcile_extension_consent failed")
