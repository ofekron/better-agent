"""Process lifecycle: SIGINT handling, startup dispatch, shutdown drain.

`on_startup` returns within milliseconds — every long-running step is
dispatched as a tracked `startup_tasks` background task. `on_shutdown` drains
them in reverse dependency order. The coordinator, schedule ticker, WS
broadcaster and build sha are injected by the composition root — see
`configure`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import extension_api
import extension_jobs
import extension_store
import file_delivery
import installation_profile
import hot_path_executor
import lag_incident_queue
import lag_watchdog
import marketplace_bridge_api
import native_index_manager
import offline_actions_api
import perf
import project_mapping_store
import project_store
import project_update_store
import provider_auth
import provider_setup
import recovery
import requirements_extension_reconciler
import session_detail_api
import session_store
import shortcut_picker
import ui_selection_projection
import virtual_session_store
from backend_instance_lock import (
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)
from event_bus import bus as event_bus
from event_ingester import event_ingester
from git_status_cache import cache as git_status_cache
from paths import ba_home
from providers_api import (
    broadcast_model_catalog_fact as _broadcast_model_catalog_fact,
    _broadcast_models_catalog_changed,
)
from queued_logging import stop_queued_logging
from run_recovery import shutdown_recovery_lease_executor
from session_listing_api import (
    forward_requirement_tags_refreshed as _forward_requirement_tags_refreshed,
)
from session_manager import manager as session_manager
from ws_serialization import reopen_ws_json_executor, shutdown_ws_json_executor

logger = logging.getLogger(__name__)

_coordinator_ref: Any = None
_schedule_ticker: Any = None
_ws_broadcaster: Any = None
_git_sha: str = "dev"


def configure(
    *,
    coordinator: Any,
    schedule_ticker: Any,
    ws_broadcaster: Any,
    git_sha: str,
) -> None:
    """Bind the process-wide collaborators startup/shutdown drive."""
    global _coordinator_ref, _schedule_ticker, _ws_broadcaster, _git_sha
    _coordinator_ref = coordinator
    _schedule_ticker = schedule_ticker
    _ws_broadcaster = ws_broadcaster
    _git_sha = git_sha


# True when the user explicitly requests shutdown (Ctrl+C / SIGINT).
# Uvicorn reload sends SIGTERM, not SIGINT, so we use this to avoid
# killing runner processes during a hot reload — they'll be re-attached
# by run_recovery on the next startup.
_intentional_shutdown = False
_uvicorn_sigint_handler = None
# Whether `on_shutdown` will kill runner subprocesses. The fail-safe
# baseline is False: restarts and ambiguous shutdowns must leave runners
# alive for run_recovery to re-attach on the next boot. Only an explicit
# affirmative prompt answer or supervisor kill flag flips this to True.
_kill_runners_on_shutdown = False
# Set by `_sigint_flag_handler` only on the SECOND (or later) SIGINT.
# `on_shutdown`'s "kill? [y/N]" prompt races against this so a second
# Ctrl+C means "stop waiting — interpret as 'n' (don't kill)" without
# requiring any I/O inside the signal handler. The runs survive; the
# next backend start picks them up via run_recovery. threading.Event
# because the prompt is awaited via
# `asyncio.to_thread(sys.stdin.readline)` — the wait happens on a
# worker thread.
_second_sigint_event = threading.Event()
# SIGINT count for the current process lifetime. The first SIGINT only
# flags intentional shutdown; only subsequent ones arm
# `_second_sigint_event`. Signal handlers run on the main thread
# between bytecodes, so a plain int suffices — no lock needed.
_sigint_count = 0
_STARTUP_ORCHESTRATOR_TASK: asyncio.Task | None = None
_model_catalog_unsubscribe: Callable[[], None] | None = None


def _shutdown_kill_runners_flag() -> Path:
    return ba_home() / "kill_runners_requested"


def _consume_shutdown_kill_runners_flag() -> bool:
    flag = _shutdown_kill_runners_flag()
    if not flag.exists():
        return False
    try:
        flag.unlink()
    except OSError:
        pass
    return True


def _sigint_flag_handler(signum, frame):
    """Signal-safe: mutate flags + chain to uvicorn. NEVER block here.

    The interactive "kill running provider processes? [y/N]" prompt
    runs inside `on_shutdown` (off the signal frame, on the event loop)
    via `asyncio.to_thread`. Doing the prompt in the signal handler
    would (1) freeze the event loop while readline blocks, (2) re-enter
    `sys.stdin.readline()` on the second Ctrl+C (uvloop redelivers
    SIGINT through `_invoke_signals`, which can fire the handler again
    while the outer readline is parked → `RuntimeError: reentrant call
    inside <BufferedReader>`), and (3) interleave its prompt bytes with
    concurrent logger output on the same stderr stream.
    """
    global _intentional_shutdown, _sigint_count
    _intentional_shutdown = True
    _sigint_count += 1
    # Only the SECOND+ SIGINT arms the abort event — the first must
    # leave it clear so `_prompt_kill_runners` can actually show the
    # prompt (sidecar: "MUST prompt the user" on Ctrl+C in an
    # interactive terminal). Once armed, `_prompt_kill_runners`
    # treats the abort as an "n" answer (don't kill subprocesses).
    # The user's intent on a
    # double-tap is "stop now without nuking my running tasks".
    if _sigint_count >= 2:
        _second_sigint_event.set()
    if callable(_uvicorn_sigint_handler):
        _uvicorn_sigint_handler(signum, frame)


async def _prompt_kill_runners() -> None:
    """Ask the user whether to kill runner subprocesses, off the signal
    frame. Sets `_kill_runners_on_shutdown` based on the answer.

    Decision matrix (TTY only — non-TTY defaults to leaving runners alive):
    - explicit "y"/"yes"             → kill
    - Enter / empty / anything else  → don't kill
    - explicit "n"/"no"              → don't kill
    - **second Ctrl+C** during prompt → don't kill (treated as "n")

    The second-Ctrl+C-as-"n" shortcut exists because users impatiently
    double-tap Ctrl+C and previously lost their long-running Claude
    runs; the safer interpretation of "I just want this to stop NOW"
    is "leave the runs alone — recovery picks them up on next start".
    See requirements-main.py.md.
    """
    global _kill_runners_on_shutdown
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        # Non-interactive (desktop .app SIGINT, `kill -INT`, containers):
        # can't ask the user, so leave runners alive — they're detached and
        # run_recovery re-attaches them on the next boot.
        _kill_runners_on_shutdown = False
        return
    if _second_sigint_event.is_set():
        # Second Ctrl+C arrived before we could even render the prompt
        # → treat as "n", don't kill the runners.
        _kill_runners_on_shutdown = False
        return
    try:
        sys.stderr.write(
            "\n^C — kill running provider processes too? "
            "[y/N]  (Ctrl+C again = n): "
        )
        sys.stderr.flush()
    except OSError:
        return
    # Race the readline against a second SIGINT. Both waits run on
    # worker threads — cancelling the asyncio Future doesn't unblock the
    # underlying thread, but the process is shutting down so leaked
    # threads are harmless.
    read_task = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
    abort_task = asyncio.create_task(asyncio.to_thread(_second_sigint_event.wait))
    try:
        done, _ = await asyncio.wait(
            {read_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task in done and read_task not in done:
            # Second Ctrl+C interrupted the prompt → "n".
            _kill_runners_on_shutdown = False
        elif read_task in done and read_task.exception() is None:
            answer = (read_task.result() or "").strip().lower()
            _kill_runners_on_shutdown = answer in ("y", "yes")
    finally:
        # task.cancel() only unwraps the asyncio Future; the underlying
        # thread keeps blocking. Set the event so abort_task's
        # `_second_sigint_event.wait` returns and its executor thread
        # exits — otherwise ThreadPoolExecutor's atexit join blocks
        # process exit forever after uvicorn prints "Finished server
        # process". The stdin thread already returned (we have an
        # answer) so it doesn't need a kick.
        _second_sigint_event.set()
        for task in (read_task, abort_task):
            if not task.done():
                task.cancel()


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine on the running event loop, logging any
    exception instead of silently swallowing it.  Replaces bare
    ``loop.create_task(coro)`` patterns that dropped errors (e.g.
    broadcast_global ValueError from a missing allowlist entry)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    async def _wrapped():
        try:
            await coro
        except Exception:
            logger.exception("fire-and-forget task failed")

    loop.create_task(_wrapped())


_TAILSCALE_SERVE_RECONCILE_INTERVAL_SECONDS = 300.0
_tailscale_serve_reconciler_task: asyncio.Task | None = None


def _tailscale_serve_reconciler_local_url() -> str | None:
    port = os.environ.get("BETTER_AGENT_BACKEND_PORT") or os.environ.get("BETTER_CLAUDE_BACKEND_PORT")
    if not port or not port.isdigit():
        return None
    return f"http://127.0.0.1:{int(port)}"


def _start_tailscale_serve_reconciler() -> None:
    """A phone's saved *.ts.net HTTPS URL dies whenever tailscaled loses its
    serve config (upgrade, reset, tailnet policy change), and no client can
    reach the backend to trigger the lazy settings-endpoint heal. Re-assert
    the config at startup and on a fixed interval."""
    global _tailscale_serve_reconciler_task
    if os.environ.get("BETTER_AGENT_TEST_MODE"):
        return
    local_url = _tailscale_serve_reconciler_local_url()
    if local_url is None:
        logger.info("tailscale serve reconciler disabled: backend port env not set")
        return

    import tailscale_https

    async def _loop() -> None:
        while True:
            try:
                await asyncio.to_thread(tailscale_https.serve_reconcile_tick, local_url)
            except Exception:
                logger.exception("tailscale serve reconcile failed")
            await asyncio.sleep(_TAILSCALE_SERVE_RECONCILE_INTERVAL_SECONDS)

    _tailscale_serve_reconciler_task = asyncio.create_task(
        _loop(), name="tailscale-serve-reconciler"
    )


_EXTENSION_UPDATE_CHECK_INTERVAL_SECONDS = 6 * 3600.0
_extension_update_checker_task: asyncio.Task | None = None


def _start_extension_update_checker() -> None:
    """Periodically refresh the remote-extension update projection so the
    frontend badge reflects marketplace/git state without polling. Pushes
    `extension_updates_changed` only when the available set changes."""
    global _extension_update_checker_task
    if (
        os.environ.get("BETTER_AGENT_TEST_MODE")
        or not installation_profile.integrations_enabled()
    ):
        return

    async def _loop() -> None:
        while True:
            try:
                previous = extension_store.cached_extension_updates()
                snapshot = await asyncio.to_thread(
                    extension_store.check_extension_updates, refresh=True,
                )
                previous_available = set((previous or {}).get("available") or [])
                available = set(snapshot.get("available") or [])
                if available != previous_available:
                    await _coordinator_ref.broadcast_global(
                        "extension_updates_changed",
                        {"available": sorted(available)},
                    )
            except Exception:
                logger.exception("extension update check failed")
            await asyncio.sleep(_EXTENSION_UPDATE_CHECK_INTERVAL_SECONDS)

    _extension_update_checker_task = asyncio.create_task(
        _loop(), name="extension-update-checker"
    )


async def on_startup():
    """Boot uvicorn fast: every long-running step (migrations,
    recovery scans, jsonl replay) runs as a tracked background task
    reported into `background_work_registry`. The frontend renders it
    in the background work manager from `GET /api/background-work` +
    `background_work_changed`; sessions still mid-recovery surface a
    per-message `isRecovering` pill from
    `session_manager._recovering_msg_ids`.

    INVARIANT: this coroutine returns within milliseconds. Anything
    that touches disk, parses jsonl, or scans subprocesses MUST be
    scheduled, not awaited inline.
    """
    await asyncio.to_thread(acquire_backend_instance_lock)
    provider_auth.reopen_status_probes()
    provider_auth.bind_config_change_loop()
    # Kill any OAuth login/logout CLI that outlived a prior backend crash
    # so no `claude auth login` / `codex login` is left holding a callback port.
    _fire_and_forget(asyncio.to_thread(provider_auth.reap_orphaned_logins))
    # Normalise a receipt written in an older encoding, so the on-disk shape
    # converges without waiting for the next activation. The read path already
    # honours the older shape; this only stops it lingering.
    await asyncio.to_thread(installation_profile.refresh_activation_receipt)
    # Freeze the capability set this process serves before anything wires
    # itself from it, so a mid-run settings change can never leave subsystems
    # disagreeing with the gates.
    await asyncio.to_thread(installation_profile.capture_active_capabilities)
    provider_runtime_enabled = installation_profile.provider_conversations_enabled()
    if not installation_profile.integrations_enabled():
        await extension_jobs.quiesce_for_ui_only()
        import extension_session_ownership
        for session_id in await asyncio.to_thread(
            extension_session_ownership.owned_session_ids
        ):
            await _coordinator_ref.cancel_session(session_id)
    if (
        not os.environ.get("BETTER_AGENT_TEST_MODE")
        and installation_profile.integrations_enabled()
    ):
        # 60s: the owner's initial storage scan is load-sensitive; a tight
        # readiness bound turns a slow-but-healthy boot into a hard
        # STARTUP_FAILURE loop (observed 2026-08-08 at machine load 100).
        await asyncio.to_thread(
            session_store.start_root_change_owner, 60.0,
        )
    session_manager.start_persistence()
    from provider import reopen_provider_tasks
    reopen_provider_tasks()
    provider_setup.reopen_provider_setup()
    reopen_ws_json_executor()
    from event_journal import event_journal_writer
    event_journal_writer.reopen()
    _coordinator_ref.reopen_prompt_admission()
    _coordinator_ref.reopen_global_broadcasts()
    logger.info("backend version=%s", _git_sha)

    # Native-transcript domain: bring up its own thread, which in turn
    # spawns the FTS index daemon. Test-mode skip lives in the manager.
    native_index_manager.manager.start()
    # Local file reads get their own thread so a large media stream
    # never stalls the request loop.
    file_delivery.host.start()

    # Install SIGINT flag so on_shutdown can distinguish Ctrl+C from
    # uvicorn reload (which sends SIGTERM, not SIGINT). The
    # `signal.signal` call only works on the main thread of the main
    # interpreter — when uvicorn is launched on a background thread
    # (integration tests do this) we skip the install rather than
    # crashing startup.
    global _uvicorn_sigint_handler, _intentional_shutdown
    global _kill_runners_on_shutdown, _sigint_count
    _intentional_shutdown = False
    _kill_runners_on_shutdown = False
    _sigint_count = 0
    offline_actions_api.open_prompt_handoffs()
    _second_sigint_event.clear()
    try:
        current = signal.getsignal(signal.SIGINT)
        if callable(current) and current is not _sigint_flag_handler:
            _uvicorn_sigint_handler = current
            signal.signal(signal.SIGINT, _sigint_flag_handler)
    except ValueError:
        # "signal only works in main thread of the main interpreter"
        logger.debug("SIGINT handler install skipped (non-main thread)")

    loop = asyncio.get_running_loop()
    _ws_broadcaster.bind(loop)

    # Perf rollup task — flushes a `PERF rollup` line every
    # ROLLUP_SECS seconds. Held at module scope inside perf.py so
    # the asyncio task isn't garbage-collected after this returns.
    perf.start_rollup_task()
    lag_incident_queue.start(lag_watchdog._dispatch_lag_watchdog_issue)
    _fire_and_forget(asyncio.to_thread(shortcut_picker.prewarm_http_stack))

    # Background running-state tick: prunes dead `_run_state` entries
    # via os.kill(pid, 0) in a daemon thread (never blocks the event
    # loop) and publishes cached running/monitoring snapshots that
    # GET /api/sessions and GET /api/projects read via
    # is_running_cached / monitoring_state_cached.
    _coordinator_ref.turn_manager.start_background_tick()
    if installation_profile.integrations_enabled():
        import operation_requests
        _fire_and_forget(operation_requests.recover())

        # Durable jobs whose owner has no recovery path are re-seeded as
        # `unknown` so a restart cannot leave the user staring at an empty
        # corner while work sits non-terminal on disk. Owners that
        # `recover()` re-registers are excluded by the seeder itself.
        async def _seed_background_work() -> None:
            import extension_jobs
            await asyncio.to_thread(extension_jobs.seed_background_work_after_recovery)

        _fire_and_forget(_seed_background_work())

    # Backend-owned schedule ticker — fires due schedules as normal
    # prompts through coordinator.submit_prompt.
    if provider_runtime_enabled:
        _schedule_ticker.start()
        import model_catalog_refresh

        global _model_catalog_unsubscribe
        if _model_catalog_unsubscribe is None:
            _model_catalog_unsubscribe = (
                model_catalog_refresh.subscribe_fact_sink(
                    _broadcast_model_catalog_fact,
                )
            )
        await model_catalog_refresh.start()

    _start_tailscale_serve_reconciler()
    _start_extension_update_checker()
    await marketplace_bridge_api.start()

    # Daily model-catalog refresher. Assumes uvicorn --workers 1
    # (see auth.py:8, run.sh:132) — a second worker would fire a
    # parallel refresh tick + double-write the cache file.
    #
    # 5-min poll: providers overdue by >=24h refresh on the next tick.
    # Worst-case latency between "model published upstream" and
    # "visible in dropdown" is THRESHOLD + POLL = 24h05m. Acceptable.
    # First iteration acts as cold-start warm-up; no explicit
    # pre-tick needed.
    #
    # Suspend-safe: `asyncio.sleep(POLL)` pauses while the host is
    # suspended; on resume, the next tick fires the wall-clock-overdue
    # providers via `last_refreshed_at + threshold < time.time()`.
    # Worst observed latency = up to POLL seconds late.
    import models as models_mod

    async def _prewarm_model_locks() -> None:
        try:
            await asyncio.to_thread(models_mod.prewarm_locks)
        except Exception:
            logger.exception("models prewarm_locks failed")

    if provider_runtime_enabled:
        asyncio.create_task(_prewarm_model_locks(), name="models-prewarm-locks")

    # Warm the get-requirements processor's provisioned base off the query
    # path — a spec version bump or restart would otherwise make the first
    # query pay the provision turn inside its dispatch budget.
    import requirement_prewarm

    async def _prewarm_requirements_processor() -> None:
        try:
            await requirement_prewarm.run_requirements_prewarm("startup")
        except Exception:
            logger.exception("requirements processor prewarm failed")

    async def _models_catalog_refresher() -> None:
        POLL = 300
        while True:
            try:
                async for pid, diff in models_mod.refresh_all_due():
                    if diff:
                        try:
                            await _broadcast_models_catalog_changed(pid, diff)
                        except Exception:
                            logger.exception(
                                "broadcast models_catalog_changed failed for %s",
                                pid,
                            )
            except Exception:
                logger.exception("models refresher error")
            await asyncio.sleep(POLL)

    if provider_runtime_enabled:
        asyncio.create_task(
            _models_catalog_refresher(),
            name="models-catalog-refresher",
        )

    async def _extension_readiness_refresher() -> None:
        while True:
            try:
                await asyncio.to_thread(extension_store.refresh_runtime_readiness_projection)
            except Exception:
                logger.exception("extension readiness projection refresh failed")
            await asyncio.sleep(2.0)

    if installation_profile.integrations_enabled():
        asyncio.create_task(
            _extension_readiness_refresher(),
            name="extension-readiness-refresher",
        )

    lag_watchdog.start()

    session_manager.bind_loop(loop)
    # DraftStore needs the loop for its debounced flush scheduling.
    # The sm hook wiring (pin_check / on_persist / on_drop) happens in
    # DraftStore.__init__ — Coordinator construction is self-sufficient.
    _coordinator_ref.draft_store.bind_loop(loop)
    from event_journal import bind_event_journal_loop
    bind_event_journal_loop(loop)
    # (`bind_active_run_gate` is wired at module-load time, right
    # after the coordinator is constructed — see main.py — so the gate
    # is in place before any route is mounted.)

    # Wire the in-process event bus's standard subscribers (persistence
    # to events.jsonl). Idempotent — safe across uvicorn reloads.
    # Sync µs-fast, stays inline.
    try:
        from event_bus_subscribers import register_default_subscribers
        register_default_subscribers()
        event_bus.unsubscribe("requirement_tags_ws")
        event_bus.subscribe(
            "requirement_tags.refreshed",
            _forward_requirement_tags_refreshed,
            priority=80,
            name="requirement_tags_ws",
        )
        await _coordinator_ref.turn_manager.lifecycle.bind()
    except Exception:
        logger.exception("event_bus subscriber registration failed")
    await _coordinator_ref.lifecycle_commands.bind()

    # Pin every subscriber registered so far to this loop. Subscribers
    # register at import time, where there is no loop to capture; until
    # they are pinned, a publish from another thread would run them on
    # the publisher's loop. Anything registered after this point stays
    # unpinned and keeps the old inline behavior, which is correct for
    # loop-agnostic handlers.
    try:
        pinned = event_bus.bind_unpinned_to_current_loop()
        logger.info("event_bus: pinned %d subscriber(s) to the main loop", pinned)
    except Exception:
        logger.exception("event_bus subscriber pinning failed")

    import project_aggregate_projection
    import session_status_projection
    await session_status_projection.publish_all_current(
        session_manager.projected_state_snapshot,
    )
    await project_aggregate_projection.flush()

    # Recovery gets its own thread, loop, and executor so a long
    # run-directory scan or replay does not compete with request
    # handlers. Started before the recovery startup task is dispatched.
    import recovery_manager
    recovery_manager.manager.start()

    # The extension UI catalog gets its own thread too: recomputing the
    # frontend projection is blocking store IO, and it must not run on
    # the loop that serves the clients it is about to notify.
    import extension_ui_manager
    extension_ui_manager.manager.start()
    extension_ui_manager.manager.bind(asyncio.get_running_loop())

    # Bind + reset the background work registry before any startup step
    # reports into it. `reset()` re-epochs, so a tab held open across a
    # uvicorn --reload discards its map instead of merging this process's
    # items into the previous process's state.
    from startup_tasks import run_task, run_composite_task
    from background_work import background_work_registry
    background_work_registry.bind(_coordinator_ref, loop)
    background_work_registry.reset()

    # Schedule every long-running step as a tracked background task.
    # `on_startup` returns the moment these are dispatched —
    # "Application startup complete" fires within milliseconds.
    from file_ref_resolver import run_migration_once
    import startup_recovery_gate
    startup_recovery_gate.begin_recovery()

    async def _on_startup_bg_orchestrator():
        """Sequence startup tasks that have ordering dependencies."""
        if not provider_runtime_enabled:
            startup_recovery_gate.mark_recovery_done()
            return
        recovery_task = asyncio.create_task(
            run_composite_task(
                "recover_in_flight",
                "startup_tasks.recover_in_flight",
                recovery._recover_in_flight_task,
            ),
            name="startup-recover-in-flight",
        )

        # Do not let unrelated maintenance compete with or precede recovery.
        await recovery_task

        if installation_profile.integrations_enabled():
            asyncio.create_task(
                run_task(
                    "requirements_projection_prewarm",
                    "startup_tasks.requirements_projection_prewarm",
                    requirement_prewarm.ensure_requirements_projection_ready,
                ),
                name="requirements-projection-prewarm",
            )

        await run_composite_task(
            "housekeeping",
            "startup_tasks.housekeeping",
            recovery._housekeeping_task,
        )

        async def _reconcile_managed_extensions() -> None:
            await recovery._run_maintenance_phase(
                "extension_store",
                extension_store.list_extensions_with_reconciliation,
                include_hidden=True,
            )

        if installation_profile.integrations_enabled():
            await run_task(
                "extension_reconciliation",
                "startup_tasks.extension_reconciliation",
                _reconcile_managed_extensions,
            )
            await run_task(
                "requirements_background_reconciliation",
                "startup_tasks.requirements_background_reconciliation",
                requirements_extension_reconciler.bind_and_reconcile,
            )
            import extension_package_loader
            try:
                await asyncio.to_thread(
                    extension_package_loader.ensure_package_importable,
                    extension_store.extension_id_for_role("requirements"),
                    "requirement_analysis",
                )
                from requirement_analysis.session_tags import bind_event_loop as bind_requirement_tags_loop
            except (extension_package_loader.ExtensionPackageUnavailable, ModuleNotFoundError):
                pass
            else:
                bind_requirement_tags_loop(loop)
            asyncio.create_task(
                run_task(
                    "requirements_processor_prewarm",
                    "startup_tasks.requirements_processor_prewarm",
                    _prewarm_requirements_processor,
                ),
                name="requirements-processor-prewarm",
            )

        from runs_dir import ensure_run_state_ledger_backfilled
        asyncio.create_task(
            run_task(
                "run_state_ledger_backfill",
                "startup_tasks.run_state_ledger_backfill",
                ensure_run_state_ledger_backfilled,
            ),
            name="startup-run-state-ledger-backfill",
        )

    # Launch and retain the orchestrator so shutdown can cancel/join it.
    global _STARTUP_ORCHESTRATOR_TASK
    _STARTUP_ORCHESTRATOR_TASK = asyncio.create_task(
        _on_startup_bg_orchestrator(), name="startup-orchestrator",
    )

    def _startup_orchestrator_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.exception(
            "startup orchestrator failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if startup_recovery_gate.is_pending():
            startup_recovery_gate.mark_recovery_failed(str(exc))

    _STARTUP_ORCHESTRATOR_TASK.add_done_callback(_startup_orchestrator_done)

    async def _delayed_startup_task(delay_s: float, task_coro_factory) -> None:
        await asyncio.sleep(delay_s)
        await task_coro_factory()

    # Backfill git remotes for existing projects + rebuild mappings.
    # Filesystem-only, independent of recovery.
    def _backfill_project_git_remotes():
        n = project_store.backfill_git_remotes()
        if n:
            logger.info("housekeeping: backfilled git_remote for %d projects", n)
        projects = project_store.list_projects()
        project_mapping_store.rebuild_and_save(projects)

    asyncio.create_task(
        run_task(
            "project_git_backfill",
            "startup_tasks.project_git_backfill",
            _backfill_project_git_remotes,
        ),
        name="startup-project-git-backfill",
    )

    # Eager-warm the session-summary index in a worker thread so the
    # first `GET /api/sessions` doesn't pay the cold-walk cost
    # (~2-5 s for 400+ session.json files). The walk MUST run off the
    # event loop — when blocked inline it starved every other
    # endpoint during startup (PERF showed /api/startup_tasks peaking
    # at 65 s, /api/sessions at 102 s, all blocked behind the lazy
    # first-call rebuild). `run_task` default `in_thread=True`
    # offloads the sync `_ensure_summary_index` via `to_thread`.
    # Independent of provider/recover tasks — filesystem-only.
    asyncio.create_task(
        run_task(
            "summary_index_warm",
            "startup_tasks.summary_index_warm",
            session_store._ensure_summary_index,
        ),
        name="startup-summary-index-warm",
    )

    asyncio.create_task(
        run_task(
            "virtual_session_summaries_warm",
            "startup_tasks.virtual_session_summaries_warm",
            virtual_session_store.list_all,
        ),
        name="startup-virtual-session-summaries-warm",
    )

    asyncio.create_task(
        run_task(
            "git_status_warm",
            "startup_tasks.git_status_warm",
            git_status_cache.warm_recent,
            in_thread=False,
        ),
        name="startup-git-status-warm",
    )

    asyncio.create_task(
        run_task(
            "project_update_counts_warm",
            "startup_tasks.project_update_counts_warm",
            project_update_store.warm_counts,
        ),
        name="startup-project-update-counts-warm",
    )

    def _warm_pending_node_projection() -> None:
        import node_link
        node_link.public_pending_nodes()

    asyncio.create_task(
        run_task(
            "pending_node_projection_warm",
            "startup_tasks.pending_node_projection_warm",
            _warm_pending_node_projection,
        ),
        name="startup-pending-node-projection-warm",
    )

    import session_search_index

    def _rebuild_session_search_index_if_empty() -> None:
        if not session_search_index.needs_rebuild():
            logger.info("session_search_index: persisted index present; skipping startup rebuild")
            return
        session_search_index.rebuild_from_disk()

    asyncio.create_task(
        _delayed_startup_task(
            20.0,
            lambda: run_task(
                "session_search_index_rebuild",
                "startup_tasks.session_search_index_rebuild",
                _rebuild_session_search_index_if_empty,
            ),
        ),
        name="startup-session-search-index-rebuild",
    )

    asyncio.create_task(
        run_task(
            "bcfile_migration",
            "startup_tasks.bcfile_migration",
            run_migration_once,
            ba_home(),
        ),
        name="startup-bcfile-migration",
    )

    if not any(
        t.get_name() == "periodic-session-auto-delete"
        for t in asyncio.all_tasks()
    ):
        async def _periodic_session_auto_delete() -> None:
            interval_s = 24 * 60 * 60
            while True:
                try:
                    await recovery._auto_delete_expired_sessions()
                    await asyncio.sleep(interval_s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("periodic session auto-delete failed")
                    await asyncio.sleep(interval_s)
        asyncio.create_task(
            _periodic_session_auto_delete(),
            name="periodic-session-auto-delete",
        )

    # Multi-machine: kick off the per-node last_acked_offset persistence
    # coalescer. Cheap (1s wakeup, idle most of the time). Only useful
    # in primary mode but harmless on the worker-node build since there
    # are no `mark_offsets_dirty` callers there.
    async def _start_node_offset_loop_if_ready() -> None:
        try:
            ready = await asyncio.to_thread(
                extension_store.is_extension_runtime_ready,
                extension_store.extension_id_for_role('machine-nodes'),
            )
            if ready:
                import node_store as _ns
                _ns.start_offset_flush_loop()
        except Exception:
            logger.exception("node_store: offset flush loop failed to start")
    asyncio.create_task(
        _start_node_offset_loop_if_ready(),
        name="node-offset-flush-startup",
    )

    # Phase-1 stage 5b: periodic internal_token rotation. Every 60 min
    # the coordinator mints a new token + retains the old one for a
    # 5min grace window. A surviving runner's run-local token authority
    # validates and loads the new value after an authentication rejection;
    # in-flight calls retry with the new token once.
    # Operators who want disabling can set
    # `BA_DISABLE_INTERNAL_TOKEN_ROTATION=1`.
    # Guard: on_startup can fire twice (uvicorn hot-reload, Starlette
    # lifespan edge-cases). Only ONE rotation task must run — a duplicate
    # clobbers `_prev_token` every ~6 min, nuking the grace window and
    # 403-ing in-flight runners.
    if not any(
        t.get_name() == "periodic-internal-token-rotation"
        for t in asyncio.all_tasks()
    ):
        async def _periodic_token_rotation() -> None:
            if os.environ.get(
                "BA_DISABLE_INTERNAL_TOKEN_ROTATION", "",
            ).strip().lower() in {"1", "true", "yes", "on"}:
                logger.info("token rotation disabled via env")
                return
            interval_s = 3600.0  # 60 minutes
            while True:
                try:
                    await asyncio.sleep(interval_s)
                    # Grace must exceed the interval so that a previous
                    # rotation's old token stays valid until the NEXT
                    # rotation preserves it as _prev_token.  2× interval
                    # gives a full rotation cycle of slack.
                    _coordinator_ref.rotate_internal_token(
                        grace_seconds=interval_s * 2,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("periodic token rotation failed")
        asyncio.create_task(
            _periodic_token_rotation(),
            name="periodic-internal-token-rotation",
        )


async def _shutdown_session_persistence_pipeline() -> None:
    import session_queue_projection

    storage_identity = session_manager._root_repository.storage_identity()
    await asyncio.to_thread(session_manager.shutdown_persistence)
    await asyncio.to_thread(session_store.shutdown_root_change_owner)
    await asyncio.to_thread(session_store.shutdown_durability_writer)
    await asyncio.to_thread(
        session_queue_projection.shutdown,
        storage_identity=storage_identity,
        certify=True,
    )


async def on_shutdown():
    """Cancel every in-flight runner on intentional shutdown (Ctrl+C).
    During uvicorn hot-reload (SIGTERM), leave runners alive — they're
    detached (start_new_session=True) and will be re-attached by
    run_recovery on the next startup. The interactive "kill? [y/N]"
    prompt lives here (not the signal handler) so it runs off the
    signal frame and can't block the event loop or re-enter readline."""
    global _kill_runners_on_shutdown, _STARTUP_ORCHESTRATOR_TASK
    global _model_catalog_unsubscribe
    offline_actions_api.close_prompt_handoffs()
    import model_catalog_refresh

    await model_catalog_refresh.shutdown()
    if _model_catalog_unsubscribe is not None:
        _model_catalog_unsubscribe()
        _model_catalog_unsubscribe = None
    startup_task = _STARTUP_ORCHESTRATOR_TASK
    _STARTUP_ORCHESTRATOR_TASK = None
    if startup_task is not None and not startup_task.done():
        startup_task.cancel()
        try:
            await startup_task
        except asyncio.CancelledError:
            pass
    try:
        import requirement_prewarm

        await requirement_prewarm.shutdown_thread_vector_projection()
    except Exception:
        logger.exception("thread vector projection shutdown failed")
    await lag_incident_queue.stop()
    await marketplace_bridge_api.stop()
    # Kill any in-flight OAuth login/logout subprocesses so a `claude auth
    # login` / `codex login` is never orphaned by a backend restart.
    try:
        await provider_auth.shutdown_status_probes()
    except Exception:
        logger.exception("provider auth status probes did not quiesce")
    finally:
        provider_auth.shutdown_all()
    try:
        await requirements_extension_reconciler.shutdown()
    except Exception:
        logger.exception("requirements extension reconciler did not quiesce")
    await extension_api.shutdown_hot_path_executors()
    from orchestrator import shutdown_auth_executor
    await shutdown_auth_executor()
    try:
        import extension_daemons

        await asyncio.to_thread(extension_daemons.shutdown_backend_daemons)
    except Exception:
        logger.exception("on_shutdown: extension_daemons shutdown failed")
    if _consume_shutdown_kill_runners_flag():
        _kill_runners_on_shutdown = True
    elif _intentional_shutdown:
        await _prompt_kill_runners()
    if _intentional_shutdown and _kill_runners_on_shutdown:
        from provider import known_providers
        try:
            killed_total = 0
            for prov in known_providers():
                # Y=kill covers in-flight turns. "Leave alive" keeps
                # everything: runners are detached, complete.json /
                # run_recovery integrates them on the next boot.
                killed_total += await asyncio.to_thread(prov.cancel_all)
            if killed_total:
                logger.info("on_shutdown: killed %d runner processes", killed_total)
        except Exception:
            logger.exception("on_shutdown: provider.cancel_all failed")
    elif _intentional_shutdown:
        logger.info("on_shutdown: user chose to leave runners alive")
    else:
        logger.info("on_shutdown: reload detected, leaving runners alive for recovery")
    await offline_actions_api._drain_prompt_handoffs()
    await _coordinator_ref.quiesce_prompt_processors()
    try:
        await asyncio.to_thread(native_index_manager.manager.stop)
    except Exception:
        logger.exception("native index manager shutdown failed")
    try:
        await asyncio.to_thread(file_delivery.host.stop)
    except Exception:
        logger.exception("file delivery host shutdown failed")
    await _schedule_ticker.shutdown()
    session_detail_api.shutdown_project_match()
    try:
        await provider_setup.shutdown_provider_setup()
    except Exception:
        logger.exception("provider setup shutdown failed")
    try:
        from provider import shutdown_provider_tasks
        await shutdown_provider_tasks()
    except Exception:
        logger.exception("provider task shutdown failed")
    await asyncio.to_thread(shutdown_recovery_lease_executor)
    try:
        import recovery_manager
        # Off the main loop: stop() joins the recovery thread, and
        # joining from the loop we are shutting down would stall it.
        await asyncio.to_thread(recovery_manager.manager.stop)
    except Exception:
        logger.exception("recovery manager shutdown failed")
    try:
        import extension_ui_manager
        await asyncio.to_thread(extension_ui_manager.manager.stop)
    except Exception:
        logger.exception("extension UI manager shutdown failed")
    hot_path_executor.shutdown_all()
    # Drain the draft-persist coalescer before closing the event
    # ingester. Drafts are kept in memory for up to DRAFT_FLUSH_DELAY
    # before hitting disk — without this drain a clean shutdown would
    # lose typed-but-unflushed draft text.
    try:
        _coordinator_ref.draft_store.drain_pending_drafts()
    except Exception:
        logger.exception("drain_pending_drafts failed")
    persistence_shutdown_error: Exception | None = None
    try:
        await _shutdown_session_persistence_pipeline()
    except Exception as exc:
        persistence_shutdown_error = exc
        logger.exception("session persistence pipeline shutdown failed")
    try:
        from event_journal import event_journal_writer
        await asyncio.to_thread(event_journal_writer.close)
    except Exception:
        logger.exception("EventJournalWriter close failed")
    try:
        import hydration_index_store
        await asyncio.to_thread(hydration_index_store.shutdown)
    except Exception:
        logger.exception("hydration index store shutdown failed")
    try:
        from event_bus_subscribers import shutdown_session_content_projection
        await asyncio.to_thread(shutdown_session_content_projection)
    except Exception:
        logger.exception("session content projection shutdown failed")
    try:
        event_ingester.close_all()
    except Exception:
        logger.exception("EventIngester close_all failed")
    try:
        await _coordinator_ref.lifecycle_commands.close()
    except Exception:
        logger.exception("lifecycle command authority shutdown failed")
    release_backend_instance_lock()
    # Multi-machine: cancel the offset coalescer + final-flush every
    # dirty node. Without this, intentional-shutdown races could leave
    # in-memory offsets stranded; the next register() would seed an
    # outdated snapshot.
    if extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes')
    ):
        try:
            import node_config_sync as _node_config_sync
            await _node_config_sync.shutdown()
        except Exception:
            logger.exception("node config sync shutdown failed")
        try:
            import node_store as _ns
            await _ns.stop_offset_flush_loop()
        except Exception:
            logger.exception("node_store: offset flush loop stop failed")
    from event_bus_subscribers import unbind_session_ws_broadcaster
    unbind_session_ws_broadcaster()
    import session_status_projection
    session_status_projection.unbind()
    import project_aggregate_projection
    await project_aggregate_projection.unbind()
    try:
        await _coordinator_ref.turn_manager.lifecycle.close()
    except Exception:
        logger.exception("lifecycle state tree shutdown failed")
    ui_selection_projection.unbind()
    await _coordinator_ref.drain_global_broadcasts()
    shutdown_ws_json_executor()
    # Last: every earlier shutdown step above still logs through the
    # queued loggers. Flush + join their listener threads only now, so
    # nothing logged during shutdown is silently dropped.
    await asyncio.to_thread(stop_queued_logging)
    if persistence_shutdown_error is not None:
        raise RuntimeError("session persistence pipeline shutdown failed") from (
            persistence_shutdown_error
        )
