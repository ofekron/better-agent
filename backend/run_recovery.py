from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import perf
from provider import RecoveredPopen
from runs_dir import pid_alive as _pid_alive, runs_root as _runs_root
from event_shape import extract_output_text as _extract_output_text
from turn_helpers import (
    _is_rate_limit_attempt,
    _is_transient_error,
    _TRANSIENT_MAX_ATTEMPTS,
)
from session_manager import manager as session_manager
from ingestion_versions import current_ingestion_version, marker_matches_current, write_marker
from redigest_backup import RedigestBackup

logger = logging.getLogger(__name__)


class _RecoveryLogSummary:
    def __init__(self) -> None:
        self.skips: Counter[str] = Counter()
        self.skip_samples: dict[str, list[str]] = defaultdict(list)
        self.not_marked: Counter[str] = Counter()
        self.not_marked_samples: dict[str, list[str]] = defaultdict(list)

    def record_skip(self, reason: str, run_id: str | None) -> None:
        self.skips[reason] += 1
        if run_id and len(self.skip_samples[reason]) < 5:
            self.skip_samples[reason].append(str(run_id)[:8])

    def record_not_marked(self, reason: str, run_id: str | None) -> None:
        self.not_marked[reason] += 1
        if run_id and len(self.not_marked_samples[reason]) < 5:
            self.not_marked_samples[reason].append(str(run_id)[:8])

    def emit(self) -> None:
        for reason, count in sorted(self.skips.items()):
            samples = ",".join(self.skip_samples.get(reason, []))
            logger.warning(
                "integrate_recovered_runs: skipped %d run(s): %s%s",
                count,
                reason,
                f" samples={samples}" if samples else "",
            )
        for reason, count in sorted(self.not_marked.items()):
            samples = ",".join(self.not_marked_samples.get(reason, []))
            logger.warning(
                "recovery: did not mark %d run(s) reconciled after %s; "
                "old ingestion version and native source is missing%s",
                count,
                reason,
                f" samples={samples}" if samples else "",
            )


def _make_unmatched_signal(
    agent_id: str, agent_type: str, description: str, sub_jsonl: Path,
) -> dict:
    """Build a `subagent_unmatched` event for a sidecar meta that
    couldn't be claimed. The `uuid` is a deterministic hash of
    (agent_id, description) so re-running recovery dedups the row at
    `event_ingester` (uid:sha256(data)) instead of appending a
    duplicate each pass."""
    import hashlib
    try:
        line_count = sum(
            1 for ln in sub_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()
        )
    except OSError:
        line_count = 0
    digest = hashlib.sha256(
        f"{agent_id}\x00{description}".encode()
    ).hexdigest()[:16]
    return {
        "type": "subagent_unmatched",
        "data": {
            "uuid": f"unmatched-{digest}",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "description": description,
            "jsonl_path": str(sub_jsonl),
            "line_count": line_count,
        },
    }


def _replay_from_claude_jsonl(
    run_dir: Path,
    *,
    unmatched_out: Optional[list[dict]] = None,
) -> list[dict]:
    """Replay this turn's events from claude CLI's session jsonl, then
    walk the `<jsonl_stem>/subagents/agent-*.jsonl` files (if any) and
    splice their events in with `parent_tool_use_id` injection so the
    subagent fan-out from the live tailer is reproduced.

    Returns a list of `{"type": "agent_message", "data": <enriched>}`
    entries — same shape `ClaudeJsonlTailer` dispatches and what the
    orchestrator stores on `assistant_msg["events"]`.

    Source of truth: `state.json` records the claude jsonl path and the
    `pre_query_byte_offset` baseline (bytes already in the file before
    this turn started). We slice from that baseline so prior turns
    aren't replayed too.
    """
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    jsonl_path_str = state.get("jsonl_path")
    if not jsonl_path_str:
        return []
    jsonl_path = Path(jsonl_path_str)
    if not jsonl_path.exists():
        return []

    try:
        pre_query_byte_offset = int(state.get("pre_query_byte_offset") or 0)
    except (TypeError, ValueError):
        pre_query_byte_offset = 0
    try:
        pre_query_inode = state.get("pre_query_jsonl_inode")
        current_stat = jsonl_path.stat()
        if pre_query_inode is not None and int(pre_query_inode) != current_stat.st_ino:
            logger.error(
                "_replay_from_claude_jsonl: baseline inode mismatch for %s "
                "(%s != %s)",
                jsonl_path, pre_query_inode, current_stat.st_ino,
            )
            return []
    except (OSError, TypeError, ValueError):
        return []

    from provider_claude import _SubagentRegistry, enrich_jsonl_line

    wrapped: list[dict] = []
    uuid_to_tool_use_ids: dict[str, list[str]] = {}
    uuid_to_parent_uuid: dict[str, str] = {}
    subagent_registry = _SubagentRegistry()

    try:
        size = current_stat.st_size
        if pre_query_byte_offset > size:
            logger.error(
                "_replay_from_claude_jsonl: baseline beyond EOF for %s "
                "(baseline=%d, size=%d)",
                jsonl_path, pre_query_byte_offset, size,
            )
            return []
        with jsonl_path.open("rb") as f:
            f.seek(pre_query_byte_offset)
            for raw_bytes in f:
                if not raw_bytes.endswith(b"\n"):
                    break
                raw = raw_bytes.decode("utf-8", errors="replace")
                ev = enrich_jsonl_line(
                    raw, uuid_to_tool_use_ids, uuid_to_parent_uuid,
                    subagent_registry,
                )
                if ev is not None:
                    wrapped.append(ev)
    except Exception:
        logger.exception(
            "_replay_from_claude_jsonl: failed reading %s", jsonl_path,
        )

    wrapped.extend(_replay_subagents(
        jsonl_path, subagent_registry, unmatched_out=unmatched_out,
    ))
    return wrapped


def _replay_subagents(
    parent_jsonl: Path, registry: "_SubagentRegistry",
    *,
    unmatched_out: Optional[list[dict]] = None,
) -> list[dict]:
    """Walk `<stem>/subagents/agent-*.meta.json` for the parent jsonl,
    claim a parent tool_use_id per subagent from the registry, and
    replay each `agent-<id>.jsonl` with that tool_use_id injected onto
    every enriched line.

    When `unmatched_out` is provided, any sidecar meta whose
    `(agentType, description)` doesn't match a pending Agent tool_use
    in `registry` (e.g. a leftover from a different run/cwd that shares
    this claude session's subagents dir) is recorded as a
    `subagent_unmatched` signal appended to that list — instead of
    being silently skipped. Callers that don't pass `unmatched_out`
    (the existing replay path + tests) keep the silent-skip behavior;
    only `_replay_and_apply` opts in and surfaces the orphans via
    `ingest_orphan`."""
    sub_dir = parent_jsonl.parent / parent_jsonl.stem / "subagents"
    if not sub_dir.exists():
        return []

    from provider_claude import enrich_jsonl_line

    out: list[dict] = []
    for meta_path in sorted(sub_dir.glob("agent-*.meta.json")):
        agent_id = meta_path.name[len("agent-") : -len(".meta.json")]
        sub_jsonl = sub_dir / f"agent-{agent_id}.jsonl"
        if not sub_jsonl.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        agent_type = meta.get("agentType", "") or ""
        description = meta.get("description", "") or ""
        parent_tuid = registry.claim(agent_type, description)
        if parent_tuid is None:
            if unmatched_out is not None:
                unmatched_out.append(
                    _make_unmatched_signal(
                        agent_id, agent_type, description, sub_jsonl,
                    )
                )
            continue
        uuid_to_tool_use_ids: dict[str, list[str]] = {}
        uuid_to_parent_uuid: dict[str, str] = {}
        try:
            with sub_jsonl.open(encoding="utf-8") as f:
                for raw in f:
                    ev = enrich_jsonl_line(
                        raw, uuid_to_tool_use_ids, uuid_to_parent_uuid,
                        registry, parent_tool_use_id=parent_tuid,
                    )
                    if ev is not None:
                        out.append(ev)
        except Exception:
            logger.exception(
                "_replay_subagents: failed reading %s", sub_jsonl,
            )

    # Workflow subagents — agents live under subagents/workflows/wf_<id>/
    wf_base = sub_dir / "workflows"
    if wf_base.exists():
        for wf_path in sorted(wf_base.iterdir()):
            if not wf_path.is_dir() or not wf_path.name.startswith("wf_"):
                continue
            run_id = wf_path.name
            if run_id not in registry._workflow_bindings:
                registry.claim_workflow(run_id)
            parent_tuid = registry.get_workflow_parent(run_id)
            if not parent_tuid:
                continue
            for meta_path in sorted(wf_path.glob("agent-*.meta.json")):
                agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                sub_jsonl = wf_path / f"agent-{agent_id}.jsonl"
                if not sub_jsonl.exists():
                    continue
                uuid_to_tool_use_ids: dict[str, list[str]] = {}
                uuid_to_parent_uuid: dict[str, str] = {}
                try:
                    with sub_jsonl.open(encoding="utf-8") as f:
                        for raw in f:
                            ev = enrich_jsonl_line(
                                raw, uuid_to_tool_use_ids, uuid_to_parent_uuid,
                                registry, parent_tool_use_id=parent_tuid,
                            )
                            if ev is not None:
                                out.append(ev)
                except Exception:
                    logger.exception(
                        "_replay_subagents: failed reading %s", sub_jsonl,
                    )

    return out


def _read_sdk_output(run_dir: Path) -> str:
    """Best-effort read of `complete.json.sdk_output` — the SDK's
    captured plaintext fallback content. Used when event replay yields
    no extractable text (matching `_finalize_turn_messages`)."""
    complete_path = run_dir / "complete.json"
    if not complete_path.exists():
        return ""
    try:
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    text = payload.get("sdk_output") if isinstance(payload, dict) else None
    return text if isinstance(text, str) else ""


def _descriptor_target_message_id(desc: dict) -> Optional[str]:
    target = desc.get("target_message_id")
    return target if isinstance(target, str) and target else None


def _assistant_by_id(sess: dict, msg_id: Optional[str]) -> Optional[dict]:
    if not msg_id:
        return None
    for msg in sess.get("messages") or []:
        if isinstance(msg, dict) and msg.get("id") == msg_id:
            return msg
    return None


def _session_key(desc: dict) -> str:
    """Group recovered runs by the app session they persist into. A run
    with no session key gets a unique per-run bucket so latest-run
    selection can never silently drop it."""
    return (
        desc.get("persist_to")
        or desc.get("app_session_id")
        or f"__no_session__:{desc.get('run_id')}"
    )


def _latest_run(descs: list[dict]) -> dict:
    """The only run in a session that may legitimately still need replay.

    INVARIANT: turns are strictly serial per session — turn N+1 cannot
    start until turn N has finalized. Therefore the sole run that can be
    in-flight/crashed-mid-turn is the most-recently-started one; every
    earlier run's events were already live-ingested onto that run's own
    assistant message. The agent CLI session jsonl is a single
    cumulative file shared by ALL of a session's runs, so replaying an
    earlier run dumps a prior turn's slice of that file onto whatever is
    currently the last assistant message — corrupting it with
    out-of-order, cross-turn events.

    Rank by run-start wall clock (`started_at`, stamped at RunState
    construction so it exists even for a run that died before computing
    `pre_query_byte_offset`); tie-break by run-dir mtime. `started_at` is
    deliberately the key and `pre_query_byte_offset` deliberately is NOT:
    a crashed latest run records `pre_query_byte_offset=0` and would
    mis-rank below an earlier completed run."""
    def key(d: dict) -> tuple[str, float]:
        try:
            mtime = (_runs_root() / (d.get("run_id") or "")).stat().st_mtime
        except OSError:
            mtime = 0.0
        return (d.get("started_at") or "", mtime)

    return max(descs, key=key)


@perf.timed_fn("run_recovery.integrate_recovered_runs")
async def integrate_recovered_runs(coordinator, recovered: list[dict]) -> None:
    """Integrate recovered runs, dispatching each to the Provider that
    owned it. The descriptor's `provider_id` is the source of truth
    (set by `_write_backend_state` at run-start). Runs whose owner no
    longer exists (config record deleted between cancel and restart)
    are marked reconciled and SKIPPED — re-binding to the active
    provider would route SIGTERM/cancel paths to a Provider whose env
    points at a different `CLAUDE_CONFIG_DIR`, leaking the orphaned
    runner subprocess.

    Replay is gated to the latest run per session (`_latest_run`).
    Every non-latest run is reconciled WITHOUT replay: its turn already
    completed and was live-ingested, so replaying its slice of the
    shared cumulative claude jsonl would leak a prior turn's events onto
    the final assistant message."""
    from provider import get_provider, default_provider

    groups: dict[str, list[dict]] = {}
    for desc in recovered:
        groups.setdefault(_session_key(desc), []).append(desc)

    summary = _RecoveryLogSummary()
    try:
        for descs in groups.values():
            # `_latest_run` does an `.stat()` per desc — sync FS I/O that
            # adds up when a single session has many turn dirs. Push it to
            # a worker thread so the event loop isn't blocked on stat
            # latency.
            latest = await asyncio.to_thread(_latest_run, descs)
            for desc in descs:
                # Per-desc yield so a long descs list (or a flood of
                # provider-cache walks) doesn't starve WS/REST handlers
                # between the heavy `_integrate_one` awaits. `sleep(0)` is
                # the cheapest yield asyncio offers.
                await asyncio.sleep(0)
                run_id = desc.get("run_id")
                if desc is not latest:
                    # Non-latest: already-finalized prior turn. Reconcile so
                    # the next scan skips it; replaying it would corrupt the
                    # final message (see `_latest_run`).
                    await _mark_reconciled_if_safe_async(
                        run_id,
                        desc,
                        "non-latest skip",
                        summary=summary,
                    )
                    continue
                try:
                    owner_id = desc.get("provider_id")
                    owner = None
                    if owner_id:
                        try:
                            owner = get_provider(owner_id)
                        except KeyError:
                            owner = None
                        # `get_provider` returns the cached instance even
                        # when the on-disk record was deleted (so callers
                        # can finish in-flight cancels). For recovery we
                        # treat defunct as "owner is gone" — re-binding to
                        # active would route SIGTERM/auth to the wrong
                        # CLAUDE_CONFIG_DIR.
                        if owner is not None and owner.defunct:
                            owner = None
                    if owner is None and owner_id and owner_id.startswith("remote:"):
                        # Remote run dir (written by RemoteProviderProxy.
                        # start_run). Proxies live outside the provider
                        # registry — resolve via provider_remote.
                        import provider_remote
                        owner = provider_remote.get_proxy(
                            owner_id.split(":", 1)[1]
                        )
                    if owner is None and not owner_id:
                        # Legacy run with no provider_id stamped — fall back
                        # to active. Best-effort for pre-binding data.
                        try:
                            owner = default_provider()
                        except Exception:
                            owner = None
                    if owner is None:
                        summary.record_skip(
                            f"owning provider {owner_id} is missing/defunct",
                            run_id,
                        )
                        await _mark_reconciled_if_safe_async(
                            run_id,
                            desc,
                            "missing provider",
                            summary=summary,
                        )
                        continue
                    await _integrate_one(coordinator, owner, desc, summary=summary)
                except Exception:
                    logger.exception("integrate_recovered_runs: failed for %s", run_id)
    finally:
        summary.emit()


def _provider_kind(desc: dict | None) -> str:
    if desc and desc.get("provider_kind"):
        return str(desc.get("provider_kind"))
    provider_id = str((desc or {}).get("provider_id") or "")
    if provider_id:
        try:
            import config_store
            rec = config_store.get_provider(provider_id)
            if rec and rec.get("kind"):
                return str(rec["kind"])
        except Exception:
            pass
    if provider_id.startswith("codex"):
        return "codex"
    return "claude"


def _touch_reconciled(run_id: str, desc: Optional[dict] = None) -> None:
    if not run_id:
        return
    try:
        write_marker(_runs_root() / run_id / "reconciled.marker", _provider_kind(desc))
    except Exception:
        logger.exception("_touch_reconciled: failed for %s", run_id)


def _ingestion_version_current(desc: dict) -> bool:
    try:
        version = int(desc.get("ingestion_version") or 0)
    except (TypeError, ValueError):
        version = 0
    return version == current_ingestion_version(_provider_kind(desc))


def _native_source_exists(desc: dict) -> bool:
    jsonl_path_str = desc.get("jsonl_path")
    if not jsonl_path_str:
        return False
    return Path(jsonl_path_str).exists()


def _can_mark_reconciled(desc: dict) -> bool:
    return _ingestion_version_current(desc) or _native_source_exists(desc)


def _mark_reconciled_if_safe(
    run_id: str,
    desc: dict,
    reason: str,
    *,
    summary: _RecoveryLogSummary | None = None,
) -> bool:
    if not _can_mark_reconciled(desc):
        if summary is not None:
            summary.record_not_marked(reason, run_id)
        else:
            logger.warning(
                "recovery: not marking %s reconciled after %s; old ingestion "
                "version and native source is missing",
                run_id,
                reason,
            )
        return False
    _touch_reconciled(run_id, desc)
    return True


async def _mark_reconciled_if_safe_async(
    run_id: str,
    desc: dict,
    reason: str,
    *,
    summary: _RecoveryLogSummary | None = None,
) -> bool:
    return await asyncio.to_thread(
        _mark_reconciled_if_safe,
        run_id,
        desc,
        reason,
        summary=summary,
    )


def _apply_recovered_stream_event_sync(
    *,
    persist_sid: str,
    run_id: str,
    mode: str,
    claude_sid: Optional[str],
    target_message_id: Optional[str],
    event: dict,
) -> None:
    event_type = event.get("type")
    data = event.get("data") or {}
    if event_type == "session_discovered":
        sid = data.get("session_id") if isinstance(data, dict) else None
        if sid:
            session_manager.set_agent_sid(
                persist_sid, mode, sid, bump_updated_at=False,
            )
        return
    if event_type in {"complete", "error"}:
        return

    with session_manager.batch(persist_sid, bump_updated_at=False):
        sess = session_manager.get_ref(persist_sid)
        if sess is None:
            return
        msg = _assistant_by_id(sess, target_message_id)
        if msg is None:
            return
        manager_sid_holder = {"id": claude_sid or sess.get("agent_session_id")}
        user_msg = _last_user_before(sess, msg)
        from orchs import ApplyEventCtx, get_strategy
        ctx = ApplyEventCtx(
            manager_sid_holder=manager_sid_holder,
            workers_list=list(msg.get("workers") or []),
            user_msg=user_msg,
            root_id=session_manager._root_id_for(persist_sid),
            run_id=run_id,
        )
        get_strategy(mode).apply_event(
            app_session_id=persist_sid,
            msg=msg,
            event=event,
            ctx=ctx,
            source_is_provider_stream=True,
        )


async def _drain_recovered_live_queue(
    coordinator,
    provider,
    desc: dict,
    queue: asyncio.Queue,
    recovering_msg_id: Optional[str],
) -> None:
    run_id = desc.get("run_id")
    app_sid = desc.get("app_session_id")
    persist_sid = desc.get("persist_to") or app_sid
    pid = desc.get("pid")
    try:
        while True:
            if (not pid or not _pid_alive(int(pid))) and queue.empty():
                break
            try:
                stream_event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            event = {"type": stream_event.type, "data": stream_event.data}
            if stream_event.type in {"complete", "error"}:
                break
            await asyncio.to_thread(
                _apply_recovered_stream_event_sync,
                persist_sid=persist_sid,
                run_id=run_id,
                mode=desc.get("mode") or "native",
                claude_sid=desc.get("session_id"),
                target_message_id=recovering_msg_id,
                event=event,
            )
    except Exception:
        logger.exception("_drain_recovered_live_queue: failed for %s", run_id)
    finally:
        await _finalize_when_done(coordinator, provider, desc, recovering_msg_id)


def _barrier_journal(persist_sid: str) -> None:
    """Block until every queued events.jsonl write for this session's
    root is durable. MUST run before `reconciled.marker` on every
    post-replay path: replay submits fire-and-forget journal writes,
    and the marker permanently gates the run out of future replays.
    Raises on timeout AND on an unresolvable root so the caller fails
    closed (no marker) — a silent return would mark the run with the
    replay's writes still queued."""
    root_id = session_manager._root_id_for(persist_sid)
    if not root_id:
        raise RuntimeError(
            f"_barrier_journal: cannot resolve root for {persist_sid}"
        )
    from event_journal import event_journal_writer
    event_journal_writer.barrier_sync(root_id)


def _events_fully_ingested(desc: dict) -> bool:
    """POSITIVE evidence that every line of this run's provider jsonl
    was already live-ingested: the persisted tailer cursor
    (`backend_state.json.processed_byte`) covers the file's current
    byte size. Anything short of that — missing cursor, missing
    jsonl, unreadable file — is NO evidence and forces a replay.
    Fail closed: over-replaying is dedup-idempotent, skipping loses
    the un-ingested tail permanently once the marker lands."""
    jsonl_path_str = desc.get("jsonl_path")
    if not jsonl_path_str:
        return False
    jsonl_path = Path(jsonl_path_str)
    if not jsonl_path.exists():
        return False
    if desc.get("provider_kind") == "codex" or desc.get("provider_id", "").startswith("codex"):
        try:
            processed_byte = int(desc.get("processed_byte_offset"))
        except (TypeError, ValueError):
            return False
        try:
            total_bytes = jsonl_path.stat().st_size
        except OSError:
            return False
        return processed_byte >= total_bytes
    try:
        processed = int(desc.get("processed_byte"))
    except (TypeError, ValueError):
        return False
    try:
        st = jsonl_path.stat()
    except OSError:
        return False
    saved_inode = desc.get("jsonl_inode")
    try:
        if saved_inode is not None and int(saved_inode) != st.st_ino:
            return False
    except (TypeError, ValueError):
        return False
    return processed >= st.st_size


def _is_consistent(sess: dict, desc: dict) -> bool:
    """Fast-path short-circuit for `_integrate_one`: True ⇒ session is
    already in the state recovery would produce AND there is positive
    evidence the run's events were fully live-ingested
    (`_events_fully_ingested`), so the integration (replay events, set
    claude_sid, etc.) can be skipped. Session-shape signals alone
    (isStreaming, stopped_at, sid stamps) cannot distinguish a cleanly
    finalized turn from a non-cancelled mid-turn crash — the sid is
    stamped per-event and isStreaming is stripped on persist — so
    absent the cursor evidence this returns False and recovery replays
    (dedup-idempotent). Reads the run's jsonl: call off the event loop.

    Post streaming-source-of-truth refactor: `isStreaming` is no
    longer persisted on disk (stripped by
    `session_store.write_session_full`). On load the in-memory msg
    has either no `isStreaming` key (new sessions) or `stopped_at`
    stamped + flag stripped (legacy sessions, via
    `_strip_legacy_isstreaming_on_load`). Either way,
    `bool(last_asst.get("isStreaming"))` is False, which is the
    correct "no runner registered yet" expectation at recovery time.

    The check works post-refactor because:
      - alive run (expected_streaming=True): disk says False, mismatch
        ⇒ integrate (hook then flips True at runner registration).
      - completed run (expected_streaming=False): disk says False,
        match ⇒ skip (msg is already finalized).
    """
    if not _ingestion_version_current(desc):
        return False

    alive = bool(desc.get("alive"))
    has_complete = bool(desc.get("has_complete_json"))
    mode = desc.get("mode") or "manager"
    claude_sid = desc.get("session_id")
    sid_field = "agent_session_id"

    last_asst = _last_assistant(sess)
    if last_asst is None:
        return False

    expected_streaming = alive and not has_complete
    if bool(last_asst.get("isStreaming")) != expected_streaming:
        return False
    # `stopped_at` is deliberately NOT checked here. It is owned by the
    # live turn path, not recovery, and `cancelled` (hard-kill) does not
    # imply a user stop — so it must not gate recovery consistency.

    if claude_sid:
        if sess.get(sid_field) != claude_sid:
            return False
        if last_asst.get("agent_session_id") is None:
            return False
    return _events_fully_ingested(desc)


async def _integrate_one(
    coordinator,
    provider,
    desc: dict,
    *,
    summary: _RecoveryLogSummary | None = None,
) -> None:
    run_id = desc.get("run_id")
    app_sid = desc.get("app_session_id")
    if not app_sid:
        if summary is not None:
            summary.record_skip("missing app_session_id", run_id)
        else:
            logger.info("integrate_recovered_runs: skip %s (no app_session_id)", run_id)
        await _mark_reconciled_if_safe_async(
            run_id,
            desc,
            "missing app session id",
            summary=summary,
        )
        return
    # `persist_to` overrides the app session when present (rare; left
    # in for forward-compat with descriptor producers that set it).
    persist_sid = desc.get("persist_to") or app_sid
    sess = await asyncio.to_thread(session_manager.get, persist_sid)
    if sess is None:
        # Session is gone — usually a user delete while the backend was
        # down (so the delete handler's run-dir reap never ran). Mark a
        # FINISHED run reconciled so it isn't re-scanned + re-logged on
        # every subsequent startup (the dir is reclaimed by the 7-day
        # age-prune). Do NOT mark a still-in-flight run: `get()` can also
        # return None transiently, and a live run that later writes
        # complete.json must stay eligible — once finished it lands here
        # again and gets marked.
        if summary is not None:
            summary.record_skip(f"session {persist_sid} missing", run_id)
        else:
            logger.info(
                "integrate_recovered_runs: skip %s (session %s missing)",
                run_id,
                persist_sid,
            )
        if bool(desc.get("has_complete_json")) or bool(desc.get("cancelled")):
            await _mark_reconciled_if_safe_async(
                run_id,
                desc,
                "missing session",
                summary=summary,
            )
        return

    alive = bool(desc.get("alive"))
    has_complete = bool(desc.get("has_complete_json"))
    cancelled = bool(desc.get("cancelled"))
    recovering_msg_id = _descriptor_target_message_id(desc)
    if recovering_msg_id and _assistant_by_id(sess, recovering_msg_id) is None:
        if summary is not None:
            summary.record_skip(f"target message {recovering_msg_id} not found", run_id)
        else:
            logger.warning(
                "integrate_recovered_runs: target message %s for run %s not found",
                recovering_msg_id,
                run_id,
        )
        recovering_msg_id = None
    if not recovering_msg_id and not (alive and not has_complete):
        last_asst = _last_assistant(sess)
        if last_asst is not None:
            fallback_id = last_asst.get("id")
            if isinstance(fallback_id, str) and fallback_id:
                recovering_msg_id = fallback_id
    if not recovering_msg_id and not (alive and not has_complete):
        if summary is not None:
            summary.record_skip("missing target_message_id", run_id)
        else:
            logger.warning(
                "integrate_recovered_runs: skip %s (missing target_message_id)",
                run_id,
            )
        return

    if not _ingestion_version_current(desc) and not await asyncio.to_thread(
        _native_source_exists,
        desc,
    ):
        if summary is not None:
            summary.record_skip(
                "old provider pipeline version and native source missing",
                run_id,
            )
        else:
            logger.warning(
                "integrate_recovered_runs: run %s was ingested with an old "
                "provider pipeline version, but native source is missing; "
                "leaving existing derived session data untouched",
                run_id,
            )
        return

    # `_is_consistent` counts jsonl lines — sync FS I/O, keep it off
    # the event loop.
    last_asst_initial = _last_assistant(sess)
    target_is_latest = bool(
        last_asst_initial and last_asst_initial.get("id") == recovering_msg_id
    )
    if target_is_latest and not (alive and not has_complete) and await asyncio.to_thread(
        _is_consistent, sess, desc,
    ):
        await _mark_reconciled_if_safe_async(run_id, desc, "consistent state")
        return

    mode = desc.get("mode") or "manager"
    claude_sid = desc.get("session_id")

    # Flip the recovering pill on the assistant message we're about to
    # mutate. Ownership of the clear is handed to `_finalize_when_done`
    # when we spawn it; otherwise the local `finally` clears it. Set the
    # `handed_off` flag the moment that task is scheduled so the finally
    # doesn't race the background task and double-clear or pre-clear.
    handed_off = False
    if recovering_msg_id:
        await asyncio.to_thread(
            session_manager.set_msg_recovering,
            persist_sid,
            recovering_msg_id,
            True,
        )

    # A re-digest (old ingestion_version + finalized dead-orphan run)
    # overwrites the render tree from the native stream. Snapshot the
    # stale-but-whole derived data first so a failed re-digest rolls
    # back instead of leaving a half-mutated tree. See
    # `redigest_backup.RedigestBackup`.
    redigest_backup: Optional["RedigestBackup"] = None
    if (
        not _ingestion_version_current(desc)
        and not alive
        and has_complete
    ):
        root_id = await asyncio.to_thread(session_manager._root_id_for, persist_sid) or persist_sid
        redigest_backup = await asyncio.to_thread(RedigestBackup(root_id).capture)

    try:
        # The batch+replay block can take seconds for sessions with
        # large claude jsonls — running it on the event loop would
        # starve every concurrent WS/REST handler for the duration.
        # The session_manager per-root lock is a `threading.RLock`, so
        # acquiring it from a worker thread is safe (cross-thread WS
        # broadcasts in the listeners are already handled by
        # `SessionWSBroadcaster._dispatch`). Loop-side spawn of
        # `_finalize_when_done` stays after the thread returns —
        # `asyncio.create_task` from a worker thread raises.
        integration_ok = True
        try:
            await asyncio.to_thread(
                _apply_integration_sync,
                persist_sid=persist_sid,
                run_id=run_id,
                mode=mode,
                claude_sid=claude_sid,
                sess=sess,
                alive=alive,
                has_complete=has_complete,
                cancelled=cancelled,
                target_message_id=recovering_msg_id,
            )
        except Exception:
            integration_ok = False
            logger.exception("integrate_recovered_runs: persist failed for %s", persist_sid)

        # NOTE: an earlier hardening pass added an explicit
        # `_dispatch_messages_delta` here to cover live frontends
        # connected during recovery (the broadcaster's allowlist
        # silently drops `running_content_updated`, see
        # session_ws_broadcaster.py:209). That dispatch required a
        # `session_manager.get(persist_sid)` deep-copy of the entire
        # session tree (up to 13 MB per session_manager.py:830) for
        # every orphan integrated at startup — measurable backend
        # boot slowdown for users with many sessions. Reverted: per
        # CLAUDE.md "scenarios 1, 2, 3 produce IDENTICAL post-load
        # state but DIFFERENT framing during the load itself", so
        # recovery-time WS framing is explicitly NOT part of the
        # convergence invariant. A frontend connected during recovery
        # picks up the finalized content on its next REST refetch or
        # subscribe (which already happens on every user navigation).

        if alive and not has_complete:
            pid = desc.get("pid")
            run_dir = _runs_root() / run_id
            queue: asyncio.Queue = asyncio.Queue()
            attached_by_provider = False
            attach_recovered = getattr(provider, "attach_recovered_run", None)
            if callable(attach_recovered):
                attached_by_provider = bool(attach_recovered(
                    desc=desc,
                    queue=queue,
                    loop=asyncio.get_running_loop(),
                ))
            if pid and run_id not in provider._runs:
                queue: asyncio.Queue = asyncio.Queue()
                stub = SimpleNamespace(
                    run_id=run_id,
                    run_dir=run_dir,
                    popen=RecoveredPopen(int(pid)),
                    mode=mode,
                    app_session_id=app_sid,
                    queue=queue,
                    session_id=claude_sid,
                    jsonl_path=Path(desc["jsonl_path"]) if desc.get("jsonl_path") else None,
                    processed_byte=int(desc.get("processed_byte") or 0),
                    started_at=datetime.now().isoformat(),
                    cancelled=cancelled,
                    persist_to=persist_sid,
                    tailer=None,
                    tailer_task=None,
                    bootstrap_task=None,
                    complete_task=None,
                    lingering=False,
                )
                provider._runs[run_id] = stub

            # Rebuild the orchestrator's _run_state so the running
            # flag (now surfaced via `session_running_changed`) reflects
            # live recovered runs immediately. Normal runs register via
            # _run_turn → run_state_add; recovery bypasses that path so
            # we register manually here.
            #
            # `active_run_ids` MUST be populated before `run_state_add`
            # so the pidless-orphan gate in `_prune_dead_entries` keeps
            # the entry alive until the PID is verified (or the turn
            # ends and `run_state_remove` fires).
            #
            # `target_message_id` is passed up-front so the streaming
            # hook in `run_state_add` flips `isStreaming=True` on the
            # rehydrated msg. The `recompute_running` hook inside
            # `run_state_add` fires `session_running_changed` so Home
            # tabs converge without polling.
            # Re-acquire the containment handle for this live run so
            # enumerate()/has_background_work()/the details tree work after
            # the restart. On macOS this rebuilds the in-memory runner_pid map
            # (else enumerate returns [] post-restart); on Linux/Windows it
            # re-opens the cgroup path / job handle.
            if pid:
                try:
                    from containment import containment
                    containment().reattach(run_id, int(pid))
                except Exception:
                    logger.warning(
                        "containment reattach failed run=%s pid=%s",
                        run_id[:8], pid, exc_info=True,
                    )
            coordinator.turn_manager.active_run_ids.setdefault(app_sid, []).append(run_id)
            coordinator.turn_manager.run_state_add(
                app_sid,
                run_id=run_id,
                kind=mode,
                target_message_id=recovering_msg_id,
                pid=int(pid) if pid else None,
            )
            # Push the updated counts to any connected Home tab so it
            # doesn't need to wait for a page refresh.
            await coordinator.turn_manager.emit_run_state(app_sid)
            logger.info(
                "integrate_recovered_runs: registered run_state for %s (alive, no complete.json)",
                run_id[:8],
            )

            if attached_by_provider:
                asyncio.create_task(
                    _drain_recovered_live_queue(
                        coordinator, provider, desc, queue, recovering_msg_id,
                    ),
                    name=f"recover-drain-{run_id[:8]}",
                )
            else:
                asyncio.create_task(
                    _finalize_when_done(coordinator, provider, desc, recovering_msg_id),
                    name=f"recover-finalize-{run_id[:8]}",
                )
            handed_off = True
        else:
            # A still-alive runner WITH complete.json is a babysitter
            # lingering for background work (it touched the `lingering`
            # sentinel). Re-register it so the kill levers
            # (lingering_runs / cancel_turn / shutdown cancel_all) and
            # the run_lingering WS projection survive the restart;
            # _watch_linger_exit deregisters it when it finally exits.
            pid = desc.get("pid")
            lr_dir = _runs_root() / run_id
            if (
                alive and pid and (lr_dir / "lingering").exists()
                and run_id not in provider._runs
                and hasattr(provider, "_watch_linger_exit")
            ):
                try:
                    from containment import containment
                    containment().reattach(run_id, int(pid))
                except Exception:
                    logger.warning(
                        "containment reattach failed run=%s pid=%s",
                        run_id[:8], pid, exc_info=True,
                    )
                stub = SimpleNamespace(
                    run_id=run_id,
                    run_dir=lr_dir,
                    popen=RecoveredPopen(int(pid)),
                    mode=mode,
                    app_session_id=app_sid,
                    queue=asyncio.Queue(),
                    session_id=claude_sid,
                    jsonl_path=Path(desc["jsonl_path"]) if desc.get("jsonl_path") else None,
                    processed_byte=int(desc.get("processed_byte") or 0),
                    started_at=datetime.now().isoformat(),
                    cancelled=cancelled,
                    persist_to=persist_sid,
                    tailer=None,
                    tailer_task=None,
                    bootstrap_task=None,
                    complete_task=None,
                    lingering=True,
                )
                provider._runs[run_id] = stub
                asyncio.create_task(
                    provider._watch_linger_exit(stub),
                    name=f"recover-linger-{run_id[:8]}",
                )
                logger.info(
                    "integrate_recovered_runs: re-registered lingering "
                    "babysitter %s (pid=%s)", run_id[:8], pid,
                )
            if not integration_ok:
                # Wholesale replay/persist failure: leave the run
                # unmarked so the next startup scan retries it. Marking
                # here would make the loss permanent and silent.
                if redigest_backup is not None:
                    await asyncio.to_thread(redigest_backup.rollback)
                logger.warning(
                    "integrate_recovered_runs: leaving %s unreconciled "
                    "for retry on next startup", run_id,
                )
                return
            # The replay's events.jsonl writes are fire-and-forget
            # (timeout=0 shard-executor submits). The marker permanently
            # gates this run out of future replays, so it must not land
            # before those writes are durable. Blocking barrier — keep
            # it off the event loop; never call it while holding the
            # root lock via batch.
            await asyncio.to_thread(_barrier_journal, persist_sid)
            await _mark_reconciled_if_safe_async(run_id, desc, "integration complete")
            if redigest_backup is not None:
                await asyncio.to_thread(redigest_backup.commit)
    finally:
        # Sync path (or any exception before handoff) clears here.
        # Once `_finalize_when_done` is scheduled it owns the clear so we
        # don't yank the pill before its replay completes.
        if recovering_msg_id and not handed_off:
            await asyncio.to_thread(
                session_manager.set_msg_recovering,
                persist_sid,
                recovering_msg_id,
                False,
            )
        # An unconsumed backup here means an exception escaped the
        # success-path tail (barrier/marker) AFTER a successful
        # re-digest — the new state on disk is good, so commit (drop the
        # snapshot). The failure path rolls back+returns before reaching
        # here, and commit/rollback both mark the backup settled.
        if redigest_backup is not None and not redigest_backup._settled:
            await asyncio.to_thread(redigest_backup.commit)


def _last_assistant(sess: dict) -> Optional[dict]:
    for m in reversed(sess.get("messages") or []):
        if m.get("role") == "assistant":
            return m
    return None


def _apply_completion_state(
    persist_sid: str,
    msg_id: str,
    *,
    cancelled: bool,
) -> None:
    """Pin the assistant msg as not-streaming at recovery completion.

    Cancelled recovered runs must carry `stopped_at` so the rendered
    message exits the in-flight UI state and exposes retry affordances.
    Non-cancelled completions clear stale `stopped_at` because they
    finished normally."""
    session_manager.set_streaming(persist_sid, msg_id, False)
    session_manager.set_stopped_at(
        persist_sid,
        msg_id,
        datetime.utcnow().isoformat() if cancelled else None,
    )


def _finalize_sync(
    *,
    persist_sid: str,
    run_id: str,
    mode: str,
    claude_sid: Optional[str],
    sess: dict,
    last_asst: dict,
    msg_id: str,
    cancelled: bool,
) -> None:
    """Thread-side body of `_finalize_when_done`'s replay +
    completion-state stamp. INVARIANT: replay and the
    `_apply_completion_state` batch must run in a single thread call
    so a concurrent finalizer for a sibling run can't sneak its own
    replay onto the event loop in the gap between them.

    bump_updated_at=False: recovery finalization re-projects the run's
    already-happened events + stamps streaming state — it is not user
    activity, so it must not bump `updated_at` and reorder the session
    in the sidebar. Mirrors `_apply_integration_sync`."""
    with session_manager.batch(persist_sid, bump_updated_at=False):
        _replay_and_apply(
            persist_sid=persist_sid,
            run_id=run_id,
            mode=mode,
            claude_sid=claude_sid,
            sess=sess,
            last_asst=last_asst,
            msg_id=msg_id,
        )
        _apply_completion_state(persist_sid, msg_id, cancelled=cancelled)


def _apply_integration_sync(
    *,
    persist_sid: str,
    run_id: str,
    mode: str,
    claude_sid: Optional[str],
    sess: dict,
    alive: bool,
    has_complete: bool,
    cancelled: bool,
    target_message_id: Optional[str],
) -> None:
    """Thread-side body of `_integrate_one`'s batch+replay. Runs
    under `asyncio.to_thread` so the event loop stays responsive
    while a large claude jsonl is replayed. INVARIANT: every
    session_manager call here is thread-safe (per-root RLock); every
    listener it fires uses the bound loop via `run_coroutine_threadsafe`
    when off the event-loop thread."""
    with session_manager.batch(persist_sid, bump_updated_at=False):
        if claude_sid:
            session_manager.set_agent_sid(
                persist_sid, mode, claude_sid, bump_updated_at=False,
            )

        live_sess = session_manager.get_ref(persist_sid) or sess
        last_asst = _assistant_by_id(live_sess, target_message_id)
        if last_asst is None:
            return
        msg_id = last_asst["id"]
        # Dead orphan: orchestrator died before finalize, so
        # events the runner produced never made it onto the
        # assistant message. Replay claude's session jsonl so
        # the work isn't lost. Idempotent: `reconciled.marker`
        # makes the next scan skip this run.
        if not alive and has_complete:
            _replay_and_apply(
                persist_sid=persist_sid,
                run_id=run_id,
                mode=mode,
                claude_sid=claude_sid,
                sess=live_sess,
                last_asst=last_asst,
                msg_id=msg_id,
            )

        # Pin the per-msg primary CLI sid if not already set.
        if claude_sid and last_asst.get("agent_session_id") is None:
            session_manager.set_agent_sid_on_msg(
                persist_sid, msg_id, claude_sid,
            )

        if alive and not has_complete:
            # `isStreaming=True` is driven by the streaming hook in
            # `coordinator.turn_manager.run_state_add` (called from `_integrate_one`
            # with `target_message_id=recovering_msg_id`), not by an
            # explicit `set_streaming` here. Clear any stale `stopped_at`
            # so a run that is resuming (alive, no complete.json) never
            # renders as "Stopped".
            session_manager.set_stopped_at(persist_sid, msg_id, None)
        else:
            _apply_completion_state(
                persist_sid,
                msg_id,
                cancelled=cancelled,
            )


def _replay_from_gemini_jsonl(run_dir: Path) -> list[dict]:
    """Replay this turn's events from the Gemini runner's normalized
    session_events.jsonl.

    Returns typed event envelopes expected by `apply_event`.
    """
    events_path = run_dir / "session_events.jsonl"
    if not events_path.exists():
        return []

    wrapped: list[dict] = []
    try:
        with events_path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    ev_data = json.loads(raw)
                    event_type = ev_data.get("type") if isinstance(ev_data, dict) else None
                    event_data = ev_data.get("data") if isinstance(ev_data, dict) else None
                    if (
                        event_type in {"agent_message", "worker_start", "worker_event", "worker_complete"}
                        and isinstance(event_data, dict)
                    ):
                        wrapped.append({"type": event_type, "data": event_data})
                    else:
                        wrapped.append({"type": "agent_message", "data": ev_data})
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception(
            "_replay_from_gemini_jsonl: failed reading %s", events_path,
        )
    return wrapped


def _replay_from_codex_rollout(run_dir: Path) -> tuple[list[dict], Optional[int]]:
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return [], None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    session_id = state.get("session_id") or ""
    jsonl_path_str = state.get("jsonl_path") or state.get("rollout_path")
    if not jsonl_path_str and session_id:
        from codex_native import resolve_rollout_path
        resolved = resolve_rollout_path(session_id)
        jsonl_path_str = str(resolved) if resolved else ""
    if not jsonl_path_str:
        return [], None
    try:
        start_byte = int(state.get("pre_query_byte_offset") or 0)
    except (TypeError, ValueError):
        start_byte = 0
    from codex_native import (
        codex_subagent_delegation_id,
        codex_subagent_sources_from_event,
        codex_subagent_rollout_start_byte,
        normalize_rollout_file,
        resolve_rollout_path,
    )
    wrapped, context_window = normalize_rollout_file(
        Path(jsonl_path_str),
        start_byte=start_byte,
        namespace=str(session_id or run_dir.name),
    )
    backend_state_path = run_dir / "backend_state.json"
    try:
        backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
    except Exception:
        backend_state = {}
    child_sources = backend_state.get("child_sources")
    if not isinstance(child_sources, dict):
        child_sources = {}
    child_sources = {
        str(k): v for k, v in child_sources.items()
        if isinstance(v, dict)
    }
    for event in wrapped:
        if event.get("type") != "agent_message":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        for subagent_source in codex_subagent_sources_from_event(data):
            source_key = subagent_source["source_key"]
            child_id = subagent_source["child_id"]
            if source_key in child_sources:
                continue
            child_path = resolve_rollout_path(child_id)
            if child_path is None:
                continue
            child_sources[source_key] = {
                "agent_id": child_id,
                "source_key": source_key,
                "parent_tool_use_id": subagent_source["parent_tool_use_id"],
                "jsonl_path": str(child_path),
                "start_byte": codex_subagent_rollout_start_byte(child_path),
                "delegation_id": subagent_source["delegation_id"],
            }
    seen_delegations: set[str] = set()
    for source_key, source in child_sources.items():
            if not isinstance(source, dict):
                continue
            child_path = source.get("jsonl_path")
            if not child_path:
                continue
            child_id = str(source.get("agent_id") or source.get("child_id") or source_key)
            parent_tool_use_id = str(source.get("parent_tool_use_id") or "")
            delegation_id = (
                source.get("delegation_id")
                or codex_subagent_delegation_id(
                    child_id,
                    parent_tool_use_id=parent_tool_use_id,
                )
            )
            if delegation_id in seen_delegations:
                continue
            seen_delegations.add(delegation_id)
            wrapped.append({"type": "worker_start", "data": {
                "delegation_id": delegation_id,
                "worker_session_id": child_id,
                "worker_description": f"Codex subagent {child_id}",
                "panel_kind": "worker",
                "is_new": False,
                "instructions_preview": "",
                "run_mode": "codex_subagent",
                "jsonl_path": child_path,
            }})
            try:
                child_start = int(source.get("start_byte") or 0)
            except (TypeError, ValueError):
                child_start = 0
            child_events, _ = normalize_rollout_file(
                Path(child_path),
                start_byte=child_start,
                namespace=str(child_id),
            )
            for child_event in child_events:
                child_event_parent_tool_use_id = (
                    child_event.get("data") or {}
                ).get("parent_tool_use_id")
                if (
                    parent_tool_use_id
                    and child_event_parent_tool_use_id
                    and child_event_parent_tool_use_id != parent_tool_use_id
                ):
                    continue
                wrapped.append({"type": "worker_event", "data": {
                    "delegation_id": delegation_id,
                    "event": child_event,
                }})
    return wrapped, context_window


def _max_event_timestamp(events: list[dict]) -> str:
    """Max `timestamp`/`ts` across replayed event data dicts.

    Claude jsonl lines keep a top-level `timestamp`; codex's normalizer
    stamps one. Events without a timestamp are skipped. Lexical compare
    is chronological for ISO-8601 strings."""
    best = ""
    for ev in events:
        data = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            continue
        ts = data.get("timestamp") or data.get("ts")
        if isinstance(ts, str) and ts > best:
            best = ts
    return best


def _repair_updated_at_to_last_activity(persist_sid: str, last_event_ts: str) -> None:
    """After re-ingestion, set `updated_at` to the session's real last-
    activity time = max(re-ingested event ts, last message ts)."""
    live = session_manager.get_ref(persist_sid)
    if live is None:
        return
    last_msg_ts = ""
    for m in reversed(live.get("messages") or []):
        ts = m.get("timestamp")
        if isinstance(ts, str) and ts:
            last_msg_ts = ts
            break
    repaired = max(last_event_ts, last_msg_ts) if (last_event_ts or last_msg_ts) else ""
    if repaired:
        session_manager.set_updated_at(persist_sid, repaired)


def _replay_and_apply(
    *,
    persist_sid: str,
    run_id: str,
    mode: str,
    claude_sid: Optional[str],
    sess: dict,
    last_asst: dict,
    msg_id: str,
) -> None:
    """Single replay+apply path shared by _integrate_one and
    _finalize_when_done. INVARIANT: must not diverge — both recovery
    scenarios apply the same uuid-deduped event stream the live ingest
    would have. Falls back to _read_sdk_output when jsonl is
    missing or text-empty.
    """
    run_dir = _runs_root() / run_id
    desc = None
    bs_path = run_dir / "backend_state.json"
    if bs_path.exists():
        try:
            desc = json.loads(bs_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Check provider kind to decide which native jsonl to replay.
    # We resolve it robustly via the provider_id in backend_state.json.
    kind = _provider_kind(desc)
    unmatched: list[dict] = []
    context_window: Optional[int] = None
    if kind == "gemini":
        all_events = _replay_from_gemini_jsonl(run_dir)
    elif kind == "codex":
        all_events, context_window = _replay_from_codex_rollout(run_dir)
    else:
        all_events = _replay_from_claude_jsonl(run_dir, unmatched_out=unmatched)

    # Real last-activity timestamp carried by the events being re-ingested
    # (claude jsonl lines keep a top-level `timestamp`; codex stamps one).
    # Used below to repair `updated_at` so a re-ingested session sorts by
    # true last activity, not a stale/spuriously-bumped value.
    last_event_ts = _max_event_timestamp(all_events)

    extracted = _extract_output_text(all_events) if all_events else ""
    if not extracted:
        # Same fallback `_finalize_turn_messages` uses — needed when
        # jsonl is missing or held no text content for this turn.
        extracted = _read_sdk_output(run_dir)
    failures = 0
    if all_events or unmatched:
        from orchs import ApplyEventCtx, get_strategy
        preceding_user = _last_user_before(sess, last_asst)
        ctx = ApplyEventCtx(
            manager_sid_holder={"id": claude_sid},
            workers_list=list(last_asst.get("workers") or []),
            user_msg=preceding_user,
            root_id=session_manager._root_id_for(persist_sid),
            run_id=run_id,
        )
        strategy = get_strategy(mode)
        # Per-event isolation: one poison event must not abort the
        # remaining replay WITHIN this attempt. But ANY failure still
        # fails the attempt as a whole (raise below) so the caller
        # never writes `reconciled.marker` over a degraded replay —
        # the run stays unmarked and the next startup retries it;
        # uuid dedup makes the already-applied events no-ops.
        for ev in all_events:
            try:
                strategy.apply_event(
                    app_session_id=persist_sid,
                    msg=last_asst,
                    event=ev,
                    ctx=ctx,
                    source_is_provider_stream=True,
                )
            except Exception:
                failures += 1
                logger.exception(
                    "_replay_and_apply: apply_event failed for run %s "
                    "(uuid=%s) — continuing with remaining events",
                    run_id, (ev.get("data") or {}).get("uuid"),
                )
        # Surface orphan sidecar metas (couldn't be claimed to any
        # Agent tool_use in this slice's registry) as `msg_id=None`
        # rows on events.jsonl — NOT on msg.events (subagent_unmatched
        # is not a render-tree etype). Deterministic uuid dedups
        # across recovery replays.
        for sig in unmatched:
            try:
                strategy.ingest_orphan(
                    app_session_id=persist_sid,
                    event=sig,
                    ctx=ctx,
                    source_is_provider_stream=True,
                )
            except Exception:
                failures += 1
                logger.exception(
                    "_replay_and_apply: ingest_orphan failed for run %s "
                    "(uuid=%s) — continuing with remaining signals",
                    run_id, (sig.get("data") or {}).get("uuid"),
                )
    if extracted:
        session_manager.update_running_content(
            persist_sid, msg_id, extracted,
        )
    if context_window:
        session_manager.set_context_window(persist_sid, context_window)
    # Repair `updated_at` to the session's real last-activity time
    # (max of the re-ingested event timestamps and the last message ts).
    # The re-ingested events carry their ORIGINAL timestamps, so this fixes
    # stale or spuriously-bumped values without reordering by re-digest time.
    # Runs inside the caller's bump=False batch; on a failed replay the
    # batch doesn't persist, so a degraded attempt can't land a bad value.
    _repair_updated_at_to_last_activity(persist_sid, last_event_ts)
    if failures:
        raise RuntimeError(
            f"_replay_and_apply: {failures} event(s) failed for run "
            f"{run_id} — failing the attempt so the run stays "
            "unreconciled for retry"
        )


def _last_user_before(sess: dict, asst: dict) -> Optional[dict]:
    msgs = sess.get("messages") or []
    try:
        idx = msgs.index(asst)
    except ValueError:
        return None
    for i in range(idx - 1, -1, -1):
        if msgs[i].get("role") == "user":
            return msgs[i]
    return None


def _should_retry_rate_limit(run_dir: Path) -> bool:
    """Check if a completed run failed with a rate-limit error.

    Tries the cheap check first (error string from complete.json) and
    only falls back to full jsonl replay if the string check is
    inconclusive.
    """
    complete_path = run_dir / "complete.json"
    if not complete_path.exists():
        return False
    try:
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if payload.get("success"):
        return False
    error = payload.get("error")
    # Fast path: most rate-limit errors produce "rate_limit" or 
    # specific Gemini status errors in the string.
    if error:
        err_lower = error.lower()
        if "rate_limit" in err_lower or "429" in err_lower:
            return True
        # Gemini-specific: "invalid session" is NOT a rate limit, but
        # might want its own recovery later. For now, keep it to 429s.

    # Slow path: check event text for rate limit markers. Detect the
    # gemini-family runners (gemini, agy, ...) by their resolved provider kind.
    desc = None
    bs_path = run_dir / "backend_state.json"
    if bs_path.exists():
        try:
            desc = json.loads(bs_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if _provider_kind(desc) == "gemini":
        events = _replay_from_gemini_jsonl(run_dir)
    else:
        events = _replay_from_claude_jsonl(run_dir)
    return _is_rate_limit_attempt(error, events)


def _should_retry_transient(
    run_dir: Path, msg: Optional[dict],
) -> bool:
    """Check if a completed run failed with a transient error that
    hasn't exhausted its retry budget. Reads the attempt counter from
    the assistant message (persisted by the orchestrator) and checks
    the error classification against ``_is_transient_error``."""
    complete_path = run_dir / "complete.json"
    if not complete_path.exists():
        return False
    try:
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if payload.get("success"):
        return False

    error = payload.get("error")
    sdk_output = payload.get("sdk_output") or ""
    # Build minimal events list so _is_transient_error can check text.
    events = []
    if sdk_output:
        events.append({"type": "agent_message", "data": {
            "type": "assistant", "text": sdk_output,
        }})

    if not _is_transient_error(error, events):
        return False

    # Check attempt budget from the persisted assistant message.
    prior = 0
    if msg:
        prior = int(msg.get("transient_attempt") or 0)
    return prior < _TRANSIENT_MAX_ATTEMPTS


async def _retry_recovered_run(
    *,
    coordinator,
    provider,
    desc: dict,
    run_dir: Path,
    app_sid: str,
    persist_sid: str,
    msg_id: str,
    recovering_msg_id: Optional[str],
) -> None:
    """Spawn a fresh runner with --resume for a recovered run that
    failed with a retriable error (rate-limit or transient)."""
    logger.info("retry for recovered run %s", desc.get("run_id"))

    # Read original inputs so we can respawn with the same params.
    input_path = run_dir / "input.json"
    try:
        inp = json.loads(input_path.read_text(encoding="utf-8")) if input_path.exists() else {}
    except Exception:
        inp = {}

    # Show the retry pill to any connected client.
    retry_at = (datetime.now() + timedelta(seconds=5)).isoformat()
    session_manager.set_msg_retrying_until(persist_sid, msg_id, retry_at)

    # Increment transient attempt counter so the next recovery pass
    # knows how many attempts have been used.
    fresh = session_manager.get(persist_sid) or {}
    last_asst = _assistant_by_id(fresh, msg_id)
    prior = int((last_asst or {}).get("transient_attempt") or 0)
    session_manager.set_msg_transient_attempt(persist_sid, msg_id, prior + 1)

    await asyncio.sleep(5)

    session_manager.set_msg_retrying_until(persist_sid, msg_id, None)

    # Re-read session to pick up the resume sid (set by
    # _integrate_one's replay or the prior run's session_discovered).
    fresh_sess = session_manager.get(persist_sid) or {}
    mode = desc.get("mode") or "native"
    resume_sid = fresh_sess.get("agent_session_id") or desc.get("session_id")

    new_run_id = str(uuid.uuid4())
    new_queue: asyncio.Queue = asyncio.Queue()
    provider.start_run(
        run_id=new_run_id,
        prompt=inp.get("prompt", ""),
        images=inp.get("images"),
        cwd=inp.get("cwd", ""),
        loop=asyncio.get_running_loop(),
        queue=new_queue,
        model=inp.get("model"),
        reasoning_effort=inp.get("reasoning_effort"),
        session_id=resume_sid,  # --resume target
        mode=mode,
        app_session_id=app_sid,
        disallowed_tools=inp.get("disallowed_tools"),
        setting_sources=inp.get("setting_sources"),
        backend_url=inp.get("backend_url"),
        internal_token=inp.get("internal_token"),
        fork=inp.get("fork", False),
        supervised=inp.get("supervised", False),
        supervisor_agent_session_id=inp.get("supervisor_agent_session_id"),
        worker_agent_session_id=inp.get("worker_agent_session_id"),
        browser_harness_enabled=inp.get("browser_harness_enabled", False),
        open_file_panel_enabled=inp.get("open_file_panel_enabled", False),
        provider_run_config=inp.get("provider_run_config"),
        capability_contexts=inp.get("capability_contexts"),
        target_message_id=msg_id,
        turn_run_id=inp.get("turn_run_id"),
    )

    new_desc = {
        "run_id": new_run_id,
        "app_session_id": app_sid,
        "persist_to": persist_sid,
        "pid": None,
        "mode": mode,
        "session_id": resume_sid,
        "jsonl_path": desc.get("jsonl_path"),
        "alive": True,
        "has_complete_json": False,
        "cancelled": False,
        "target_message_id": msg_id,
        "turn_run_id": inp.get("turn_run_id"),
    }

    # Register the retried run in `active_run_ids` + `_run_state` so the
    # running-state signal is live and `_prune_dead_entries` doesn't
    # drop the pidless entry. Mirrors `_integrate_one`'s registration.
    coordinator.turn_manager.active_run_ids.setdefault(app_sid, []).append(new_run_id)
    provider_rs = provider._runs.get(new_run_id)
    pid = provider_rs.popen.pid if provider_rs and provider_rs.popen else None
    coordinator.turn_manager.run_state_add(
        app_sid,
        run_id=new_run_id,
        kind=mode,
        target_message_id=recovering_msg_id,
        pid=int(pid) if pid else None,
    )
    await coordinator.turn_manager.emit_run_state(app_sid)

    asyncio.create_task(
        _finalize_when_done(coordinator, provider, new_desc, recovering_msg_id),
        name=f"recover-finalize-{new_run_id[:8]}",
    )


def _cleanup_active_run_id(coordinator, app_sid: str, run_id: str) -> None:
    """Remove a recovered run's `run_id` from `active_run_ids` so
    `_prune_dead_entries` can resume pruning the session. No-op if
    already removed (e.g. by `_session_cancel_and_cleanup`)."""
    rids = coordinator.turn_manager.active_run_ids.get(app_sid)
    if rids and run_id in rids:
        rids.remove(run_id)
        if not rids:
            coordinator.turn_manager.active_run_ids.pop(app_sid, None)


def _recovery_target_snapshot(
    persist_sid: str,
    recovering_msg_id: Optional[str],
) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    sess = session_manager.get(persist_sid)
    if sess is None:
        return None, None, None
    last_asst = (
        _assistant_by_id(sess, recovering_msg_id)
        if recovering_msg_id is not None
        else _last_assistant(sess)
    )
    msg_id = last_asst.get("id") if isinstance(last_asst, dict) else None
    return sess, last_asst, msg_id


async def _finalize_when_done(
    coordinator, provider, desc: dict, recovering_msg_id: Optional[str] = None,
) -> None:
    run_id = desc.get("run_id")
    app_sid = desc.get("app_session_id")
    pid = desc.get("pid")
    persist_sid = desc.get("persist_to") or app_sid
    run_dir = _runs_root() / run_id
    complete_path = run_dir / "complete.json"
    backend_state_path = run_dir / "backend_state.json"
    try:
        while True:
            if complete_path.exists():
                break
            if not pid or not _pid_alive(int(pid)):
                break
            await asyncio.sleep(2.0)

        await asyncio.sleep(1.0)

        sess, last_asst, msg_id = await asyncio.to_thread(
            _recovery_target_snapshot,
            persist_sid,
            recovering_msg_id,
        )
        if sess is None:
            provider._runs.pop(run_id, None)
            coordinator.turn_manager.run_state_remove(app_sid, run_id)
            await coordinator.turn_manager.emit_run_state(app_sid)
            return

        cancelled = False
        if backend_state_path.exists():
            try:
                bs = json.loads(backend_state_path.read_text(encoding="utf-8"))
                cancelled = bool(bs.get("cancelled", False))
            except Exception:
                pass

        finalize_ok = True
        if last_asst is None:
            finalize_ok = False
            logger.warning(
                "_finalize_when_done: no recovery target for run %s; "
                "leaving unreconciled",
                run_id,
            )
        else:
            # Replay + completion-state are pushed to a worker thread
            # as one block — splitting them would let another finalizer
            # (running concurrently for a sibling run) replay on the
            # loop in the gap. UUID dedup in apply_event +
            # event_ingester keeps replay idempotent for events already
            # processed by the live tailer before the backend restart.
            try:
                await asyncio.to_thread(
                    _finalize_sync,
                    persist_sid=persist_sid,
                    run_id=run_id,
                    mode=desc.get("mode") or "native",
                    claude_sid=desc.get("session_id"),
                    sess=sess,
                    last_asst=last_asst,
                    msg_id=msg_id,
                    cancelled=cancelled,
                )
            except Exception:
                finalize_ok = False
                logger.exception("_finalize_when_done: persist failed for %s", persist_sid)

            # See note in `_integrate_one` above — recovery-time
            # `messages_delta` dispatch was reverted because the
            # required `session_manager.get` deep-copies the full
            # session tree per orphan and measurably slows backend
            # boot. Recovery-time WS framing isn't part of CLAUDE.md's
            # convergence invariant; live frontends pick up finalized
            # content on next REST refetch.

            # --- Rate-limit retry for recovered runs ---
            # When a run that was in-flight during a backend crash completes
            # with a rate-limit error, respawn a fresh runner with --resume
            # so the retry survives across backend restarts.
            if not cancelled and _should_retry_rate_limit(run_dir):
                # Remove old recovered run from state before retry spawns
                # a new one via provider.start_run (which adds its own).
                provider._runs.pop(run_id, None)
                coordinator.turn_manager.run_state_remove(app_sid, run_id)
                await _retry_recovered_run(
                    coordinator=coordinator,
                    provider=provider,
                    desc=desc,
                    run_dir=run_dir,
                    app_sid=app_sid,
                    persist_sid=persist_sid,
                    msg_id=msg_id,
                    recovering_msg_id=recovering_msg_id,
                )
                return  # new task owns cleanup

            # --- Transient-error retry for recovered runs ---
            # Same pattern as rate-limit: the run failed with a transient
            # error (timeout, network glitch, etc.) and the attempt budget
            # hasn't been exhausted.
            _, last_asst_now, _ = await asyncio.to_thread(
                _recovery_target_snapshot,
                persist_sid,
                recovering_msg_id,
            )
            if (
                not cancelled
                and _should_retry_transient(run_dir, last_asst_now)
            ):
                logger.info(
                    "transient-error retry for recovered run %s", run_id,
                )
                provider._runs.pop(run_id, None)
                coordinator.turn_manager.run_state_remove(app_sid, run_id)
                await _retry_recovered_run(
                    coordinator=coordinator,
                    provider=provider,
                    desc=desc,
                    run_dir=run_dir,
                    app_sid=app_sid,
                    persist_sid=persist_sid,
                    msg_id=msg_id,
                    recovering_msg_id=recovering_msg_id,
                )
                return  # new task owns cleanup

        provider._runs.pop(run_id, None)
        coordinator.turn_manager.run_state_remove(app_sid, run_id)
        await coordinator.turn_manager.emit_run_state(app_sid)
        if not finalize_ok:
            # Wholesale replay/persist failure: leave the run unmarked
            # so the next startup scan retries it.
            logger.warning(
                "_finalize_when_done: leaving %s unreconciled for retry "
                "on next startup", run_id,
            )
            return
        # Barrier before marker — see `_barrier_journal`.
        await asyncio.to_thread(_barrier_journal, persist_sid)
        _mark_reconciled_if_safe(run_id, desc, "finalize complete")
    except asyncio.CancelledError:
        # Backend shutdown (Ctrl+C) cancelled this finalizer mid-flight.
        # `asyncio.to_thread` can't propagate cancellation into the
        # worker thread, so `_finalize_sync` may still be writing to
        # session_manager. Swallow + re-raise so the cancel ladder
        # unwinds, but skip the post-loop cleanup below — the loop is
        # closing and pill-clear broadcasts would scream into a closed
        # loop.
        raise
    except Exception:
        logger.exception("_finalize_when_done: failed for %s", run_id)
    finally:
        # Always clean up `active_run_ids` for this run — regardless of
        # success, retry, cancellation, or exception.  Without this the
        # entry leaks and `_prune_dead_entries` stops pruning the session
        # until the next live turn or backend restart.
        _cleanup_active_run_id(coordinator, app_sid, run_id)
        if recovering_msg_id and persist_sid:
            # During shutdown the WS broadcast scheduled by
            # `set_msg_recovering` lands on a closing loop —
            # `_dispatch` already swallows RuntimeError, but the
            # underlying session_manager mutation itself can also fail
            # if the in-memory cache was torn down. Guard so an
            # interrupted shutdown doesn't bury the real cancel
            # reason under a clear-pill traceback.
            try:
                await asyncio.to_thread(
                    session_manager.set_msg_recovering,
                    persist_sid,
                    recovering_msg_id,
                    False,
                )
            except Exception:
                logger.debug(
                    "_finalize_when_done: clear pill failed during teardown "
                    "for %s", persist_sid,
                )


# ============================================================================
# Remote-run recovery (multi-machine)
#
# Remote runs cannot be classified at startup-scan time — the node must
# be online. `integrate_remote_runs_for_node` runs whenever a node
# (re)connects (node_store "connected" listener wired in main.py) and
# whenever a terminal run_control arrives for a run primary no longer
# tracks (provider_remote._on_run_control). It classifies each pending
# primary-side remote run dir via the node's `get_run_status` RPC:
#
#   alive            → `rehook_run`: the node rebuilds its shipping ctx
#                      and re-ships events (UUID-dedup absorbs the
#                      replay); integration happens later when the
#                      terminal run_control lands.
#   complete / dead  → materialize complete.json locally, rebuild the
#                      shadow jsonl from the node (`read_run_jsonl`
#                      pages), synthesize state.json pointing at the
#                      shadow with the node's pre_query_byte_offset
#                      baseline, then feed the standard
#                      `integrate_recovered_runs` funnel (latest-run
#                      gating + `_integrate_one` replay included).
#
# Known limitation: subagent sidecar jsonls (`<stem>/subagents/`) are
# not fetched from the node, so a recovered remote turn replays without
# subagent fan-out events. Dedup-safe; tracked as a follow-up.
# ============================================================================
_remote_coordinator = None


def set_remote_recovery_coordinator(coordinator) -> None:
    """Stash the coordinator for connection-triggered remote recovery.
    Called once at startup from main.py."""
    global _remote_coordinator
    _remote_coordinator = coordinator


async def integrate_remote_runs_for_node(
    node_id: str,
    run_id_filter: Optional[set[str]] = None,
) -> None:
    coordinator = _remote_coordinator
    if coordinator is None:
        logger.warning(
            "integrate_remote_runs_for_node: coordinator not set; "
            "skipping recovery for node %s", node_id,
        )
        return
    root = _runs_root()
    if not root.exists():
        return
    pending: list[tuple[Path, dict]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if run_id_filter is not None and child.name not in run_id_filter:
            continue
        bs_path = child / "backend_state.json"
        if not bs_path.exists():
            continue
        try:
            bs = json.loads(bs_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        marker_kind = _provider_kind(bs)
        if marker_matches_current(child / "reconciled.marker", marker_kind):
            continue
        if bs.get("node_id") != node_id:
            continue
        pending.append((child, bs))
    if not pending:
        return

    import node_link
    try:
        res = await node_link.rpc_call(
            node_id, "get_run_status",
            {"run_ids": [child.name for child, _ in pending]},
            timeout=60.0,
        )
    except Exception:
        logger.warning(
            "integrate_remote_runs_for_node: get_run_status failed for "
            "node %s — retrying on its next connect", node_id, exc_info=True,
        )
        return
    statuses = (res or {}).get("runs") or {}

    descs: list[dict] = []
    for child, bs in pending:
        await asyncio.sleep(0)
        st = statuses.get(child.name) or {"exists": False}
        try:
            desc = await _prepare_remote_desc(node_id, child, bs, st)
        except Exception:
            logger.exception(
                "integrate_remote_runs_for_node: prepare failed for %s",
                child.name,
            )
            continue
        if desc is not None:
            descs.append(desc)
    if descs:
        await integrate_recovered_runs(coordinator, descs)


async def _prepare_remote_desc(
    node_id: str, run_dir: Path, bs: dict, st: dict,
) -> Optional[dict]:
    """Classify one pending remote run dir against the node's status.
    Returns a descriptor for the standard integration funnel, or None
    when the run is still alive (rehooked; integrated on terminal)."""
    from runs_dir import atomic_write_json
    run_id = run_dir.name
    complete = st.get("complete")
    if st.get("exists") and st.get("alive") and complete is None:
        import node_link
        try:
            await node_link.send_rehook_run(node_id, run_id)
        except Exception:
            logger.warning(
                "_prepare_remote_desc: rehook_run send failed for %s",
                run_id, exc_info=True,
            )
        return None

    if not (run_dir / "complete.json").exists():
        if complete is None:
            complete = {
                "success": False,
                "session_id": st.get("session_id") or bs.get("session_id"),
                "error": (
                    "run not found on node (pruned or spawn lost)"
                    if not st.get("exists")
                    else "runner died on node before completion (recovered)"
                ),
                "token_usage": None,
                "finished_at": datetime.now().isoformat(),
            }
        atomic_write_json(run_dir / "complete.json", complete)

    sid = (
        st.get("session_id")
        or (complete or {}).get("session_id")
        or bs.get("session_id")
    )
    root_id = bs.get("root_id")
    shadow: Optional[Path] = None
    if sid and root_id and st.get("exists"):
        try:
            shadow = await _refresh_shadow_from_node(
                node_id, run_id, root_id, sid,
            )
        except Exception:
            logger.warning(
                "_prepare_remote_desc: shadow refresh failed for %s",
                run_id, exc_info=True,
            )
    if shadow is not None:
        atomic_write_json(run_dir / "state.json", {
            "session_id": sid,
            "jsonl_path": str(shadow),
            "pre_query_byte_offset": int(st.get("pre_query_byte_offset") or 0),
        })

    return {
        "run_id": run_id,
        "pid": None,
        "alive": False,
        "session_id": sid,
        "jsonl_path": str(shadow) if shadow is not None else None,
        "app_session_id": bs.get("app_session_id"),
        "persist_to": bs.get("persist_to") or bs.get("app_session_id"),
        "started_at": bs.get("started_at") or "",
        "processed_byte": 0,
        "cancelled": bool(bs.get("cancelled", False)),
        "mode": bs.get("mode"),
        "has_complete_json": True,
        "provider_id": bs.get("provider_id") or f"remote:{node_id}",
        "provider_kind": bs.get("provider_kind") or _provider_kind(bs),
        "ingestion_version": bs.get("ingestion_version"),
        "target_message_id": bs.get("target_message_id"),
        "turn_run_id": bs.get("turn_run_id"),
    }


async def _refresh_shadow_from_node(
    node_id: str, run_id: str, root_id: str, sid: str,
) -> Optional[Path]:
    """Pull the node's claude session jsonl for this run (paged) and
    rebuild the primary-side shadow file from it — the authoritative
    replay source for `_replay_from_claude_jsonl`."""
    import node_link
    import shadow_jsonl
    lines: list[str] = []
    start = 0
    while True:
        res = await node_link.rpc_call(
            node_id, "read_run_jsonl",
            {"run_id": run_id, "start_line": start},
            timeout=60.0,
        )
        page = (res or {}).get("lines") or []
        lines.extend(page)
        start = (res or {}).get("next_line", start + len(page))
        if (res or {}).get("eof", True):
            break
        if start > 500_000:
            logger.warning(
                "_refresh_shadow_from_node: %s exceeded 500k lines; "
                "truncating fetch", run_id,
            )
            break
    if not lines:
        return None
    return await shadow_jsonl.rebuild(root_id, sid, "\n".join(lines) + "\n")
