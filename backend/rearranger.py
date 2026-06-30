"""Experimental Rearranger — side Claude CLI session that extracts a
hierarchical JSON tree of user intent from a better-agent session.

Lifecycle overview
------------------

- ONE global bootstrap session is created lazily the first time any UI
  session enables the feature. Stored in rearranger_state.json.
- Each UI session that enables the feature forks off the bootstrap on
  its first rearrangement, producing a per-session rearranger sid that
  lives on the session record (`rearranger_session_id`).
- Every subsequent rearrangement forks off the per-session sid,
  producing a new per-session sid that replaces the stored one. The
  fork chain keeps the rearranger's prior-tree memory alive without
  growing a single session monotonically.
- Rearrangements are triggered two ways per enabled session:
    1. A periodic asyncio ticker (~20s) while the feature is on.
    2. A final trigger fired by the orchestrator right after
       `turn_complete` / `turn_stopped`.
  A per-session lock serialises them so they never overlap.

CLI spawn
---------

The rearranger does NOT use the streaming `start_run` path — that's
tightly coupled to manager/worker event streaming. Instead each run
goes through `default_provider().run_headless(...)` (a one-shot
`claude -p --output-format json` with `--permission-mode bypassPermissions`).
Stdout is a single JSON envelope:

    {
      "type": "result",
      "is_error": false,
      "result": "<assistant text>",
      "session_id": "<new forked sid>",
      "usage": {...}
    }

We parse that, pull `result` (which should itself be the tree JSON),
validate the tree, persist it, and broadcast a `rearranger_updated`
event to the UI.
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from paths import ba_home
from typing import Any, Optional

import rearranger_state
import llm_call_log
import trace_collector
from provider import default_provider, get_provider
from session_manager import manager as session_manager
from rearranger_prompt import BOOTSTRAP_PROMPT, build_diff_prompt, project_trace_steps

logger = logging.getLogger(__name__)


# How long a subprocess is allowed to run before we kill it. Rearranger
# prompts are small (one diff, one JSON tree) so this is generous.
_CLI_TIMEOUT_SECONDS = 180.0

# Max tree depth enforced by the validator. The bootstrap prompt also
# tells the rearranger about this cap.
_MAX_DEPTH = 3


def _load_new_trace_steps(new_messages: list[dict]) -> list[dict]:
    """Load every trace referenced by a new assistant message, project
    its steps down to compact dicts, and return the flattened list.

    Runs synchronously — expected to be called via asyncio.to_thread
    from the rearranger's coroutine so disk I/O doesn't block the
    event loop. Missing / unreadable traces are skipped silently;
    this is advisory data for the rearranger, not load-bearing.
    """
    traces: list[dict] = []
    seen_trace_ids: set[str] = set()
    for msg in new_messages:
        tid = msg.get("trace_id")
        if not tid or tid in seen_trace_ids:
            continue
        seen_trace_ids.add(tid)
        trace = trace_collector.get_trace(tid)
        if trace:
            traces.append(trace)
    return project_trace_steps(traces)


# ============================================================================
# Tree schema validation
# ============================================================================

def _validate_tree(tree: Any) -> bool:
    """Return True if `tree` matches the rearranger JSON schema.

    Shallow, permissive: we trim strings on persist rather than reject,
    but we do require the overall `{root: {...}}` envelope, recursive
    `children` arrays with sane `level` integers, and (if present)
    `trace_refs` arrays of `{trace_id: str, step_index: int}` dicts.
    """
    if not isinstance(tree, dict):
        return False
    root = tree.get("root")
    if not isinstance(root, dict):
        return False

    def _valid_trace_refs(refs: Any) -> bool:
        if refs is None:
            return True  # field is optional
        if not isinstance(refs, list):
            return False
        for r in refs:
            if not isinstance(r, dict):
                return False
            if not isinstance(r.get("trace_id"), str):
                return False
            if not isinstance(r.get("step_index"), int):
                return False
        return True

    def _walk(node: Any, depth: int) -> bool:
        if not isinstance(node, dict):
            return False
        if not isinstance(node.get("title", ""), str):
            return False
        if not isinstance(node.get("summary", ""), str):
            return False
        level = node.get("level")
        if not isinstance(level, int) or level < 0 or level > _MAX_DEPTH:
            return False
        if not _valid_trace_refs(node.get("trace_refs")):
            return False
        children = node.get("children", [])
        if not isinstance(children, list):
            return False
        if depth >= _MAX_DEPTH and children:
            # Enforce the depth cap structurally.
            return False
        return all(_walk(c, depth + 1) for c in children)

    return _walk(root, 0)


# ============================================================================
# Rearranger
# ============================================================================

class Rearranger:
    """Per-session rearranger coordinator.

    One instance is created at backend startup and wired into `main.py`.
    It owns:
      - a per-session ticker task (while the feature is enabled),
      - a per-session asyncio.Lock (serialises ticks and final triggers),
      - a per-session WS callback (so we can push `rearranger_updated`
        events to the frontend outside of a turn),
      - a single bootstrap lock (serialises concurrent first-uses across
        different UI sessions).
    """

    def __init__(
        self,
        session_manager_module,
        coordinator=None,
        *,
        tick_interval: float = 20.0,
    ) -> None:
        self._store = session_manager_module
        self._coordinator = coordinator
        self._tick_interval = tick_interval

        self._tickers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._bootstrap_lock = asyncio.Lock()

        # Max consecutive tick failures before we auto-disable the
        # feature for a session and tell the frontend.
        self._failure_counts: dict[str, int] = {}
        self._max_failures = 3

    def _provider(self):
        """Pin every rearranger CLI call to the provider that minted
        the global bootstrap. The bootstrap's claude jsonl lives under
        that provider's `CLAUDE_CONFIG_DIR`, so all forks off it must
        run under the same provider — switching the active provider
        mid-life would orphan the bootstrap.

        If the pinned provider was deleted (or its cached instance is
        defunct), we CLEAR the bootstrap sid AND every session's
        per-session `rearranger_session_id` so the next call re-mints
        from scratch under whatever's active now. The per-session
        sids forked off the dead bootstrap and would fail to resume
        under any other provider's config dir — must be discarded
        together with the bootstrap.
        """
        pinned = rearranger_state.get_bootstrap_provider_id()
        if pinned:
            prov = None
            try:
                prov = get_provider(pinned)
            except KeyError:
                prov = None
            if prov is not None and not prov.defunct:
                return prov
            logger.warning(
                "rearranger: pinned provider %s missing/defunct — clearing "
                "bootstrap + per-session sids to force re-mint under active",
                pinned,
            )
            rearranger_state.clear_bootstrap_session_id()
            try:
                cleared = self._store.clear_all_rearranger_sids()
                if cleared:
                    logger.info(
                        "rearranger: cleared %d stale per-session sid(s)", cleared,
                    )
            except Exception:
                logger.exception(
                    "rearranger: failed to clear per-session sids after "
                    "bootstrap re-mint",
                )
        provider = default_provider()
        if getattr(provider, "suspended", False):
            raise RuntimeError("default provider is suspended")
        return provider

    def set_coordinator(self, coordinator) -> None:
        """Late-bind the coordinator to avoid a constructor cycle in main.py."""
        self._coordinator = coordinator

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------
    async def set_enabled(self, app_session_id: str, enabled: bool) -> None:
        """Toggle the feature for `app_session_id`. Safe to call mid-turn."""
        session = self._store.get(app_session_id)
        if not session:
            logger.warning("rearranger.set_enabled: unknown session %s", app_session_id)
            return

        session_manager.set_rearranger_enabled(app_session_id, enabled)

        if enabled:
            self._locks.setdefault(app_session_id, asyncio.Lock())
            self._failure_counts[app_session_id] = 0
            if app_session_id not in self._tickers or self._tickers[app_session_id].done():
                self._tickers[app_session_id] = asyncio.create_task(
                    self._ticker_loop(app_session_id),
                    name=f"rearranger-ticker-{app_session_id[:8]}",
                )
                logger.info("rearranger: ticker started for %s", app_session_id)
        else:
            await self._cancel_ticker(app_session_id)
            logger.info("rearranger: ticker stopped for %s", app_session_id)

        await self._broadcast(
            app_session_id,
            {
                "type": "rearranger_state",
                "data": {"app_session_id": app_session_id, "enabled": enabled},
            },
        )

    async def trigger_final(self, app_session_id: str) -> None:
        """Fire one immediate rearrangement after a turn completes.

        Called via `asyncio.create_task` from the orchestrator so it
        doesn't block `turn_complete` emission. No-op if the feature is
        disabled for this session.
        """
        session = self._store.get(app_session_id)
        if not session or not session.get("rearranger_enabled"):
            return

        lock = self._locks.setdefault(app_session_id, asyncio.Lock())
        async with lock:
            try:
                await self._run_once(app_session_id)
            except Exception:
                logger.exception(
                    "rearranger trigger_final failed for %s", app_session_id
                )

    async def stop(self, app_session_id: str) -> None:
        """Stop and clean up all state for a session (used on delete)."""
        await self._cancel_ticker(app_session_id)
        self._locks.pop(app_session_id, None)
        self._failure_counts.pop(app_session_id, None)

    async def shutdown(self) -> None:
        """Cancel every ticker, await them with a short timeout."""
        tasks = list(self._tickers.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self._tickers.clear()
        self._locks.clear()

    # ------------------------------------------------------------------
    # Ticker loop
    # ------------------------------------------------------------------
    async def _cancel_ticker(self, app_session_id: str) -> None:
        task = self._tickers.pop(app_session_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _ticker_loop(self, app_session_id: str) -> None:
        """Periodic tick loop for one session.

        Runs ONCE immediately (so the user sees a tree right after
        enabling the feature, without waiting a full tick_interval),
        then sleeps `tick_interval` between subsequent runs. A tick is
        a no-op when the source session has no new messages since the
        last rearrangement. Three consecutive failures disable the
        feature.
        """
        first_pass = True
        try:
            while True:
                if not first_pass:
                    await asyncio.sleep(self._tick_interval)
                first_pass = False
                session = self._store.get(app_session_id)
                if not session or not session.get("rearranger_enabled"):
                    return
                lock = self._locks.setdefault(app_session_id, asyncio.Lock())
                async with lock:
                    try:
                        await self._run_once(app_session_id)
                        self._failure_counts[app_session_id] = 0
                    except Exception:
                        logger.exception(
                            "rearranger tick failed for %s", app_session_id
                        )
                        self._failure_counts[app_session_id] = (
                            self._failure_counts.get(app_session_id, 0) + 1
                        )
                        if (
                            self._failure_counts[app_session_id]
                            >= self._max_failures
                        ):
                            logger.error(
                                "rearranger: %d consecutive failures for %s — disabling",
                                self._max_failures, app_session_id,
                            )
                            session_manager.set_rearranger_enabled(
                                app_session_id, False,
                            )
                            await self._broadcast(
                                app_session_id,
                                {
                                    "type": "rearranger_state",
                                    "data": {
                                        "app_session_id": app_session_id,
                                        "enabled": False,
                                    },
                                },
                            )
                            return
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Core run
    # ------------------------------------------------------------------
    async def _run_once(self, app_session_id: str) -> None:
        """Run exactly one rearrangement pass for a session.

        Assumes the caller holds the per-session lock. Loads the source
        session, computes the diff, spawns the CLI, validates the
        returned tree, and persists both the tree and the new per-session
        fork sid.
        """
        session = self._store.get(app_session_id)
        if not session:
            return

        last_count = int(session.get("rearranger_last_message_count") or 0)
        messages = session.get("messages") or []
        total_count = len(messages)

        if total_count <= last_count:
            # Nothing new — skip silently.
            return

        new_messages = messages[last_count:]
        source_path = str(
            ba_home() / "sessions" / f"{app_session_id}.json"
        )

        # Load the full trace for every new assistant message that
        # carries a trace_id, then project each trace's steps down to
        # the compact shape the rearranger expects. This is the leaf-
        # level material the rearranger will re-parent into the tree.
        new_trace_steps = await asyncio.to_thread(
            _load_new_trace_steps, new_messages
        )

        diff_prompt = build_diff_prompt(
            source_path=source_path,
            new_messages=new_messages,
            total_message_count=total_count,
            previous_message_count=last_count,
            new_trace_steps=new_trace_steps,
        )

        # Ensure the global bootstrap exists.
        bootstrap_sid = await self._ensure_bootstrap()
        if not bootstrap_sid:
            logger.error("rearranger: bootstrap unavailable, aborting run")
            return

        # Decide what to resume from.
        prior_session_sid = session.get("rearranger_session_id")
        resume_sid = prior_session_sid or bootstrap_sid

        # Rearranger needs `fork=True` so each run BRANCHES off the
        # bootstrap (or prior rearranger session) without mutating the
        # shared global bootstrap state. Providers without fork
        # support (gemini-cli 0.42) would silently CORRUPT the
        # bootstrap if we ran without fork — every rearranger run would
        # write into the same session. Skip the run entirely instead.
        provider = self._provider()
        if not provider.supports_fork:
            logger.warning(
                "rearranger: skipping run — provider %s does not support "
                "fork; running without fork would corrupt the shared "
                "rearranger bootstrap session", provider.KIND,
            )
            return

        result = await provider.run_headless(
            prompt=diff_prompt,
            resume_sid=resume_sid,
            fork=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        if result is None:
            return

        # Accumulate this run's cost onto the session's rearranger
        # stats AND the session's grand-total token usage. Do this even
        # on tree-parse / validation failures below: the CLI already
        # spent the tokens regardless of whether we liked the output.
        # `add_rearranger_usage` returns the post-mutation session so
        # downstream broadcasts can ship the updated stats without
        # a separate read-back.
        usage = result.get("usage") or {}
        cost_usd = result.get("total_cost_usd")
        updated = session_manager.add_rearranger_usage(
            app_session_id, usage, cost_usd,
        )
        try:
            await asyncio.to_thread(
                llm_call_log.append_call,
                source="rearranger",
                reason="session_tree_projection",
                provider_id=provider.id,
                provider_kind=provider.KIND,
                provider_name=provider.record.get("name"),
                model=provider.record.get("default_model"),
                app_session_id=app_session_id,
                provider_session_id=result.get("session_id"),
                prompt=diff_prompt,
                token_usage=usage,
                success=not bool(result.get("is_error")),
                error=result.get("error"),
            )
        except Exception:
            logger.exception("failed to append rearranger llm call log")

        tree_text = result.get("result") or ""
        new_sid = result.get("session_id")

        # Parse the tree JSON the rearranger emitted.
        try:
            tree = json.loads(tree_text)
        except (json.JSONDecodeError, TypeError):
            # Sometimes models wrap in ```json ... ```; strip fences and retry.
            stripped = tree_text.strip()
            if stripped.startswith("```"):
                # Remove first fence line, drop trailing fence.
                lines = stripped.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                stripped = "\n".join(lines).strip()
            try:
                tree = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "rearranger: output not valid JSON for %s: %r",
                    app_session_id, tree_text[:200],
                )
                return

        if not _validate_tree(tree):
            logger.warning(
                "rearranger: tree failed schema validation for %s", app_session_id
            )
            # Still broadcast the updated stats so the UI sees the cost
            # this run incurred even if the tree was rejected.
            await self._broadcast_stats_only(app_session_id, updated)
            return

        # Persist tree + new fork sid + last_message_count. Returns the
        # post-mutation session — used right below for the WS broadcast
        # so no separate get() reach-back is needed.
        updated = session_manager.set_rearranger_run(
            app_session_id,
            tree=tree,
            agent_sid=new_sid or prior_session_sid,
            last_message_count=total_count,
        ) or {}
        await self._broadcast(
            app_session_id,
            {
                "type": "rearranger_updated",
                "data": {
                    "app_session_id": app_session_id,
                    "tree": tree,
                    "rearranger_session_id": new_sid or prior_session_sid,
                    "last_message_count": total_count,
                    "rearranger_stats": updated.get("rearranger_stats"),
                    "token_usage_total": updated.get("token_usage_total"),
                    "token_usage_last": updated.get("token_usage_last"),
                },
            },
        )
        logger.info(
            "rearranger: %s updated (%d new messages, new sid %s, cost $%s)",
            app_session_id, len(new_messages), (new_sid or "")[:8],
            f"{cost_usd:.4f}" if isinstance(cost_usd, (int, float)) else "?",
        )

    async def _broadcast_stats_only(
        self,
        app_session_id: str,
        updated: Optional[dict] = None,
    ) -> None:
        """Push just the updated rearranger_stats + token_usage_total to
        the frontend — used when a run incurred cost but the resulting
        tree was rejected (schema violation / parse error). Without this
        the UI's breakdown would silently diverge from the session file.

        `updated` is the post-mutation session snapshot from the
        triggering mutation (e.g. `add_rearranger_usage`'s return).
        Falls back to a fresh `get()` only when the caller didn't
        capture one — every in-tree caller now passes it.
        """
        if updated is None:
            updated = self._store.get(app_session_id) or {}
        await self._broadcast(
            app_session_id,
            {
                "type": "rearranger_updated",
                "data": {
                    "app_session_id": app_session_id,
                    "rearranger_stats": updated.get("rearranger_stats"),
                    "token_usage_total": updated.get("token_usage_total"),
                    "token_usage_last": updated.get("token_usage_last"),
                },
            },
        )

    # ------------------------------------------------------------------
    # Bootstrap (lazy, global, once)
    # ------------------------------------------------------------------
    async def _ensure_bootstrap(self) -> Optional[str]:
        """Return the global bootstrap session id, creating it if needed.

        The `_bootstrap_lock` serialises concurrent first-uses so we
        never spawn two bootstraps in parallel. If the CLI call fails
        we log and return None; the caller aborts the run.
        """
        sid = rearranger_state.get_bootstrap_session_id()
        if sid:
            return sid

        async with self._bootstrap_lock:
            # Re-check — another coroutine may have bootstrapped while
            # we were waiting for the lock.
            sid = rearranger_state.get_bootstrap_session_id()
            if sid:
                return sid

            new_sid = str(uuid.uuid4())
            logger.info(
                "rearranger: bootstrapping global session with sid=%s", new_sid
            )
            # Mint under the currently-active provider and pin so all
            # subsequent rearranger calls resolve back to the same one.
            mint_provider = default_provider()
            if getattr(mint_provider, "suspended", False):
                logger.warning("rearranger: skipping bootstrap — default provider is suspended")
                return None
            result = await mint_provider.run_headless(
                prompt=BOOTSTRAP_PROMPT,
                session_id=new_sid,
                resume_sid=None,
                fork=False,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
            if result is None or result.get("is_error"):
                logger.error(
                    "rearranger: bootstrap failed: %s",
                    (result or {}).get("result") or "no output",
                )
                return None

            # The CLI may re-assign the session id internally; trust the
            # value in the result envelope.
            final_sid = result.get("session_id") or new_sid
            rearranger_state.set_bootstrap_session_id(
                final_sid, provider_id=mint_provider.id,
            )
            logger.info(
                "rearranger: bootstrap complete, sid=%s, provider=%s",
                final_sid, mint_provider.id,
            )
            return final_sid

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------
    async def _broadcast(self, app_session_id: str, event: dict) -> None:
        """Persist to events.jsonl. The tailer's per-sid WS fan-out
        (`BetterAgentJsonlTailer`) delivers it to every connected
        client — same path REST snapshot replay uses on reconnect, so
        no gap and no duplicate."""
        etype = event.get("type")
        data = event.get("data") or {}
        if not (isinstance(etype, str) and isinstance(data, dict)):
            return
        try:
            from event_journal import publish_event
            root_id = session_manager._root_id_for(app_session_id)
            if root_id:
                await publish_event(
                    session_id=root_id,
                    context_id=app_session_id,
                    event_type=etype,
                    data=data,
                    source="rearranger",
                )
        except Exception:
            logger.exception(
                "rearranger: ingest failed for %s type=%s",
                app_session_id, etype,
            )
