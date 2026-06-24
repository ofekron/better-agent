"""Node-side handlers for messages from primary.

Runs on the worker-node process. Drives the local `ClaudeProvider` to
spawn real claude subprocesses; pumps the resulting `StreamEvent`
queue + raw claude jsonl onto the node→primary WS link.

INVARIANT (single-code-path): claude→StreamEvent translation lives
entirely in the local `ClaudeProvider`. This module is transport.

Each remote run carries a `root_id` (assigned by primary's session_store).
The node uses that root_id directly when persisting events to its
local `ba_home()/sessions/<root_id>/events.jsonl` — the node has no
session_store record; it just ingests events into a directory whose
existence is enough to keep ingestion working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchs.jsonl_helpers import compute_jsonl_path
from env_compat import get_env
from provider import StreamEvent, default_provider

logger = logging.getLogger(__name__)


@dataclass
class _RemoteRunCtx:
    run_id: str
    root_id: str
    worker_agent_session_id: str
    cwd: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    drain_task: Optional[asyncio.Task] = None
    jsonl_path: Optional[Path] = None
    jsonl_watcher_task: Optional[asyncio.Task] = None
    file_version: int = 0
    _jsonl_buf: bytes = b""


_ctx_by_run: dict[str, _RemoteRunCtx] = {}


# ============================================================================
# spawn_run
# ============================================================================
async def handle_spawn_run(node_client, msg: dict) -> None:
    run_id = msg.get("run_id")
    root_id = msg.get("root_id")
    worker_agent_session_id = msg.get("worker_agent_session_id") or msg.get("app_session_id")
    cwd = msg.get("cwd")
    if not all([run_id, root_id, cwd, worker_agent_session_id]):
        logger.error("node_rpc: spawn_run missing required field: %r", msg)
        return

    ctx = _RemoteRunCtx(
        run_id=run_id,
        root_id=root_id,
        worker_agent_session_id=worker_agent_session_id,
        cwd=cwd,
    )
    _ctx_by_run[run_id] = ctx

    loop = asyncio.get_running_loop()
    provider = default_provider()
    try:
        import startup_recovery_gate
        await startup_recovery_gate.wait_for_recovery_ready()
        provider.start_run(
            run_id=run_id,
            prompt=msg["prompt"],
            cwd=cwd,
            loop=loop,
            queue=ctx.queue,
            model=msg.get("model"),
            reasoning_effort=msg.get("reasoning_effort"),
            session_id=msg.get("session_id"),
            mode=msg.get("mode") or "native",
            app_session_id=worker_agent_session_id,
            disallowed_tools=msg.get("disallowed_tools"),
            setting_sources=msg.get("setting_sources"),
            backend_url=msg.get("backend_url"),
            internal_token=msg.get("internal_token"),
            fork=bool(msg.get("fork")),
            supervised=bool(msg.get("supervised")),
            supervisor_agent_session_id=msg.get("supervisor_agent_session_id"),
            worker_agent_session_id=worker_agent_session_id,
            mssg_sender_session_id=msg.get("mssg_sender_session_id"),
            is_worker=bool(msg.get("is_worker")),
            browser_test_enabled=bool(msg.get("browser_test_enabled")),
            open_file_panel_enabled=bool(msg.get("open_file_panel_enabled")),
            extra_env=msg.get("extra_env"),
            provider_run_config=msg.get("provider_run_config"),
            capability_contexts=msg.get("capability_contexts"),
            target_message_id=msg.get("target_message_id"),
            turn_run_id=msg.get("turn_run_id"),
            disabled_builtin_extensions=msg.get("disabled_builtin_extensions"),
            files=msg.get("files"),
        )
    except Exception as e:
        logger.exception("node_rpc: provider.start_run failed run=%s", run_id)
        await node_client.send_run_control(
            run_id=run_id,
            control_type="error",
            data={"error": f"{type(e).__name__}: {e}"},
        )
        _ctx_by_run.pop(run_id, None)
        return

    # Persist the spawn context next to the provider's run dir so a
    # node restart can rebuild this ctx and re-ship the run's events
    # when primary asks (`rehook_run`). NOT the spawn payload itself —
    # no prompt, no internal_token.
    try:
        from runs_dir import atomic_write_json, runs_root
        rd = runs_root() / run_id
        rd.mkdir(parents=True, exist_ok=True)
        atomic_write_json(rd / "remote_ctx.json", {
            "root_id": root_id,
            "worker_agent_session_id": worker_agent_session_id,
            "cwd": cwd,
        })
    except Exception:
        logger.exception("node_rpc: failed to persist remote_ctx for %s", run_id)

    ctx.drain_task = asyncio.create_task(
        _drain_queue(node_client, ctx), name=f"drain-{run_id[:8]}",
    )


# ============================================================================
# restart
# ============================================================================
async def handle_restart(node_client, msg: dict) -> None:
    """Gracefully shut down and re-exec the node process.

    Uses ``os.execv`` so the process is replaced in-place — no orphan,
    no PID change for process supervisors. The new process reconnects
    to primary with the same identity.
    """
    logger.info("node_rpc: restart requested — re-execing process")
    import sys
    import signal

    # Give the WS a moment to flush the ack, then exec.
    await node_client.stop()
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def _drain_queue(node_client, ctx: _RemoteRunCtx) -> None:
    try:
        while True:
            event: StreamEvent = await ctx.queue.get()

            if event.type == "session_discovered":
                disc_sid = event.data.get("session_id")
                if disc_sid and ctx.jsonl_path is None:
                    ctx.jsonl_path = compute_jsonl_path(ctx.cwd, disc_sid)
                    if ctx.jsonl_path:
                        ctx.jsonl_watcher_task = asyncio.create_task(
                            _ship_jsonl_lines(node_client, ctx, disc_sid),
                            name=f"jsonl-ship-{ctx.run_id[:8]}",
                        )
                await node_client.send_run_control(
                    run_id=ctx.run_id,
                    control_type="session_discovered",
                    data=event.data,
                )
                continue

            if event.type in ("complete", "error"):
                # Persist locally too: keep node's events.jsonl as
                # truth-of-record. Skip the control-only events that
                # _subprocess_agent also skips.
                await node_client.send_run_control(
                    run_id=ctx.run_id,
                    control_type=event.type,
                    data=event.data,
                )
                break

            # Persist locally (node's events.jsonl) AND ship to primary.
            # The journal writer is UUID-deduped so this is idempotent
            # against later re-reads from the same tailer.
            if event.type == "agent_message":
                try:
                    from event_journal import publish_event
                    await publish_event(
                        session_id=ctx.root_id,
                        context_id=ctx.worker_agent_session_id,
                        event_type="agent_message",
                        data=event.data,
                        source="node_local",
                    )
                except Exception:
                    logger.exception(
                        "node_rpc: local journal write failed run=%s",
                        ctx.run_id,
                    )

            await node_client.send_event_forward(
                root_id=ctx.root_id,
                sid=ctx.worker_agent_session_id,
                event_type=event.type,
                data=event.data,
                source=f"remote_node:{_local_node_id()}",
                run_id=ctx.run_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("node_rpc: drain crashed for run=%s", ctx.run_id)
    finally:
        if ctx.jsonl_watcher_task is not None:
            ctx.jsonl_watcher_task.cancel()
        _ctx_by_run.pop(ctx.run_id, None)


async def _ship_jsonl_lines(
    node_client, ctx: _RemoteRunCtx, fork_agent_sid: str,
) -> None:
    """Tail the worker's claude jsonl on this node; ship raw lines to
    primary. `file_version` bumps when the source file rotates (size
    shrinks below the last-read offset)."""
    path = ctx.jsonl_path
    if path is None:
        return
    for _ in range(50):
        if path.exists():
            break
        await asyncio.sleep(0.1)
    if not path.exists():
        logger.warning("node_rpc: jsonl never appeared at %s", path)
        return

    line_offset_in_version = 0
    fh = open(path, "rb")
    ctx.file_version = max(ctx.file_version, 1)
    try:
        while True:
            pos = fh.tell()
            current_size = path.stat().st_size if path.exists() else 0
            if current_size < pos:
                # Rotation/truncation — bump version, rewind.
                ctx.file_version += 1
                line_offset_in_version = 0
                ctx._jsonl_buf = b""
                fh.close()
                fh = open(path, "rb")
            chunk = fh.read()
            if chunk:
                buf = ctx._jsonl_buf + chunk
                parts = buf.split(b"\n")
                ctx._jsonl_buf = parts[-1]  # last partial line stays
                for full in parts[:-1]:
                    full_str = full.decode("utf-8", errors="replace")
                    await node_client.send_jsonl_line(
                        root_id=ctx.root_id,
                        fork_agent_sid=fork_agent_sid,
                        file_version=ctx.file_version,
                        line_offset_in_version=line_offset_in_version,
                        line=full_str,
                    )
                    line_offset_in_version += len(full) + 1
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        raise
    finally:
        fh.close()


# ============================================================================
# rehook_run — primary lost its drain state (restart) but the run is
# still alive on this node. Rebuild a ctx from the persisted
# remote_ctx.json and re-ship the run's events from its run-dir
# events.jsonl, through the SAME _drain_queue shipping path a live
# spawn uses. Primary-side UUID dedup makes the from-zero replay a
# no-op for everything it already ingested.
# ============================================================================
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


async def handle_rehook_run(node_client, msg: dict) -> None:
    run_id = msg.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        logger.warning("node_rpc: rehook_run invalid run_id %r", run_id)
        return
    if run_id in _ctx_by_run:
        # Live drain is already shipping — nothing to rebuild.
        return
    from runs_dir import runs_root
    rd = runs_root() / run_id
    try:
        import json as _json
        meta = _json.loads((rd / "remote_ctx.json").read_text(encoding="utf-8"))
    except Exception:
        logger.warning("node_rpc: rehook_run %s — no readable remote_ctx.json", run_id)
        return
    ctx = _RemoteRunCtx(
        run_id=run_id,
        root_id=meta.get("root_id") or "",
        worker_agent_session_id=meta.get("worker_agent_session_id") or "",
        cwd=meta.get("cwd") or "",
    )
    if not all([ctx.root_id, ctx.worker_agent_session_id, ctx.cwd]):
        logger.warning("node_rpc: rehook_run %s — incomplete remote_ctx", run_id)
        return
    _ctx_by_run[run_id] = ctx
    ctx.drain_task = asyncio.create_task(
        _drain_queue(node_client, ctx), name=f"rehook-drain-{run_id[:8]}",
    )
    asyncio.create_task(
        _tail_run_events_into_queue(ctx, rd),
        name=f"rehook-tail-{run_id[:8]}",
    )
    logger.info("node_rpc: rehooked run %s", run_id)


async def _tail_run_events_into_queue(ctx: _RemoteRunCtx, run_dir: Path) -> None:
    """File-tail producer for a rehooked run: feed the run-dir
    events.jsonl into ctx.queue as StreamEvents so `_drain_queue`
    ships them exactly like a live provider queue. Terminates after
    enqueuing a terminal event; synthesizes one from complete.json
    (or a dead runner) when the runner died before writing it."""
    import json as _json
    from runs_dir import pid_alive as _pid_alive

    events_path = run_dir / "events.jsonl"
    pid: Optional[int] = None
    try:
        pid = int((run_dir / "pid").read_text().strip())
    except Exception:
        pass

    fh = None
    buf = b""
    try:
        while True:
            if fh is None and events_path.exists():
                fh = open(events_path, "rb")
            saw_terminal = False
            if fh is not None:
                chunk = fh.read()
                if chunk:
                    buf += chunk
                    parts = buf.split(b"\n")
                    buf = parts[-1]
                    for raw in parts[:-1]:
                        try:
                            ev = _json.loads(raw.decode("utf-8", errors="replace"))
                        except Exception:
                            continue
                        etype = ev.get("type")
                        if not etype:
                            continue
                        await ctx.queue.put(
                            StreamEvent(type=etype, data=ev.get("data") or {})
                        )
                        if etype in ("complete", "error"):
                            saw_terminal = True
                else:
                    fh.seek(fh.tell())  # clear cached EOF
            if saw_terminal:
                return
            complete_path = run_dir / "complete.json"
            drained = fh is not None and not buf
            if drained and complete_path.exists():
                # Runner finished and the events file is fully shipped,
                # but no terminal line was found (runner died between
                # complete.json and the terminal event line, or the
                # line predates this tail). Synthesize from complete.json.
                try:
                    payload = _json.loads(complete_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {"success": False, "error": "unreadable complete.json"}
                etype = "complete" if payload.get("success") else "error"
                await ctx.queue.put(StreamEvent(type=etype, data=payload))
                return
            if drained and pid and not _pid_alive(pid):
                await ctx.queue.put(StreamEvent(type="error", data={
                    "success": False,
                    "error": "runner died on node before completion (rehook)",
                }))
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("node_rpc: rehook tail crashed for %s", ctx.run_id)
    finally:
        if fh is not None:
            fh.close()


# ============================================================================
# cancel_run
# ============================================================================
async def handle_cancel_run(node_client, msg: dict) -> None:
    run_id = msg.get("run_id")
    if not run_id:
        return
    provider = default_provider()
    try:
        provider.cancel_run(run_id)
    except Exception:
        logger.exception("node_rpc: cancel_run failed run=%s", run_id)


# ============================================================================
# resume_stream
# ============================================================================
async def handle_resume_stream(node_client, msg: dict) -> None:
    """v1: log + ignore. UUID dedup on primary makes replay idempotent.
    Active drain tasks continue shipping new events as they arrive;
    primary dedupes anything it already saw. Explicit
    replay-from-offset is a follow-up."""
    logger.info(
        "node_rpc: resume_stream last_acked=%s shadow=%s",
        msg.get("last_acked"), msg.get("shadow_jsonls"),
    )


# ============================================================================
# Generic RPCs — filesystem ops dispatched by primary's `_file_op` helper.
#
# Each handler runs on the node whose `dispatch_rpc` was invoked (either
# via the inbound WS path in `node_client._handle_rpc_request`, or
# in-process when primary's `_file_op` short-circuits the local case).
# Defense-in-depth: every path-receiving handler runs
# `_assert_within_cwd_roots` before touching the filesystem, so even if
# the primary forwards a bogus path, the node refuses to step outside
# its declared `cwd_roots` allowlist.
# ============================================================================
async def dispatch_rpc(method: str, params: dict) -> dict:
    """Single node-side RPC entry. Sync handlers run off-loop via
    `to_thread`; async handlers (provider run ops like `run_headless`
    / `rewind`) are awaited directly on the loop."""
    handler = _HANDLERS.get(method)
    if handler is None:
        raise ValueError(f"unknown rpc method: {method!r}")
    if asyncio.iscoroutinefunction(handler):
        return await handler(params)
    return await asyncio.to_thread(handler, params)


async def call_local_or_remote(node_id: str, method: str, params: dict):
    """Route an RPC to the local `dispatch_rpc` (in-process) when
    `node_id` is the local sentinel `"primary"` or matches the local
    topology id; otherwise ship over `node_link.rpc_call` to the
    remote node. Raises plain exceptions (FileNotFoundError,
    ValueError, RuntimeError, etc.) — callers that need HTTP status
    translation wrap this themselves (see `main._file_op`)."""
    if node_id == "primary":
        return await dispatch_rpc(method, params)
    try:
        from topology import local_node_id as _lid
        local_id = _lid()
    except Exception:
        raise RuntimeError(
            f"node_id={node_id!r} requires topology.yaml; "
            f"BETTER_CLAUDE_TOPOLOGY_PATH not configured"
        )
    if node_id == local_id:
        return await dispatch_rpc(method, params)
    import node_link
    return await node_link.rpc_call(node_id, method, params)


# INVARIANT: matches any absolute path (POSIX or Windows) excluding NUL
# and control characters. Loosened from the original ASCII-only filter so
# that real-world paths with spaces, parens, plus signs, unicode (Mac
# iCloud Drive, Hebrew folder names) work. Windows support adds two more
# absolute forms: drive-letter (`C:/…` or `C:\…`) and UNC (`\\server\…`),
# since a POSIX-only `^/…` rule rejects every Windows path. Path traversal
# (`..`) is blocked separately by `_assert_within_cwd_roots` and the
# OS-level resolve() in handlers.
_SAFE_PATH_RE = re.compile(
    r"^(?:"
    r"/[^\x00-\x1f]+"                  # POSIX absolute            (/home/me)
    r"|[A-Za-z]:[\\/][^\x00-\x1f]*"    # Windows drive-letter      (C:/Users, C:\Users)
    r"|\\\\[^\x00-\x1f]+"              # Windows UNC               (\\server\share)
    r")$"
)


def _validate_path(path_str: str) -> None:
    if not isinstance(path_str, str) or not path_str:
        raise ValueError("path must be a non-empty string")
    if not _SAFE_PATH_RE.match(path_str):
        raise ValueError(f"invalid path: {path_str!r}")


def _assert_within_cwd_roots(path_str: str) -> None:
    """INVARIANT: when this process is a worker-node with a declared
    `cwd_roots` allowlist, every RPC-served filesystem path MUST sit
    under one of those roots. Skipped silently in two cases:
      (1) `topology.yaml` is not configured (single-machine deploy —
          primary calls `_file_op("primary", ...)` which bypasses the
          wire entirely; the in-process dispatch_rpc call still passes
          through here but no allowlist exists yet).
      (2) The local node declares empty `cwd_roots` (= wildcard;
          intentional for primaries that hold no path restriction)."""
    if not path_str:
        return
    try:
        from topology import load_topology
        spec = load_topology().get(_local_node_id())
    except Exception:
        return
    if not spec.cwd_roots:
        return
    if not any(
        path_str == root or path_str.startswith(root.rstrip("/") + "/")
        for root in spec.cwd_roots
    ):
        raise ValueError(
            f"path {path_str!r} is outside this node's cwd_roots "
            f"{list(spec.cwd_roots)}"
        )


# ---- handlers --------------------------------------------------------

def _rpc_list_dir(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    p = Path(path_str)
    if not p.is_dir():
        raise FileNotFoundError(f"not a directory: {path_str}")
    entries = []
    for child in sorted(p.iterdir()):
        try:
            entries.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            })
        except OSError:
            continue
    return {"path": path_str, "entries": entries}


def _rpc_list_directories(params: dict) -> dict:
    path_str = params.get("path") or ""
    if path_str:
        _validate_path(path_str)
        _assert_within_cwd_roots(path_str)
    from file_browser import list_directories
    return list_directories(path_str)


def _rpc_get_file_tree(params: dict) -> dict:
    root = params.get("root") or ""
    _validate_path(root)
    _assert_within_cwd_roots(root)
    max_depth = int(params.get("max_depth") or 3)
    from file_browser import get_file_tree
    return get_file_tree(root, max_depth=max_depth)


def _rpc_search_tree(params: dict) -> dict:
    root = params.get("root") or ""
    _validate_path(root)
    _assert_within_cwd_roots(root)
    from file_browser import search_tree
    return search_tree(
        root=root,
        query=params.get("query") or "",
        kind=params.get("kind") or "file",
        methods=tuple(params.get("methods") or ("path", "name", "symbols")),
    )


def _rpc_get_file_content(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import get_file_content
    return get_file_content(path_str)


def _rpc_get_file_metadata(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import get_file_metadata
    return get_file_metadata(path_str)


def _rpc_write_file_content(params: dict) -> dict:
    path_str = params.get("path") or ""
    content = params.get("content")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import write_file_content
    return write_file_content(path_str, content)


def _rpc_create_file(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import create_file
    return create_file(path_str)


def _rpc_create_directory(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import create_directory
    return create_directory(path_str)


def _rpc_reconstruct_before_edit(params: dict) -> dict:
    path_str = params.get("file_path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import reconstruct_before_edit
    return reconstruct_before_edit(
        path_str,
        params.get("old_string") or "",
        params.get("new_string") or "",
    )


def _rpc_get_git_status(params: dict) -> dict:
    cwd = params.get("cwd") or ""
    _validate_path(cwd)
    _assert_within_cwd_roots(cwd)
    from file_browser import get_git_status
    return get_git_status(cwd)


def _rpc_get_file_diff(params: dict) -> dict:
    file_path = params.get("file_path") or ""
    cwd = params.get("cwd") or ""
    _validate_path(file_path)
    _validate_path(cwd)
    _assert_within_cwd_roots(file_path)
    _assert_within_cwd_roots(cwd)
    from file_browser import get_file_diff
    return {"diff": get_file_diff(file_path, cwd)}


def _rpc_git_commit(params: dict) -> dict:
    cwd = params.get("cwd") or ""
    message = params.get("message") or ""
    _validate_path(cwd)
    _assert_within_cwd_roots(cwd)
    if not message.strip():
        return {"ok": False, "error": "Commit message cannot be empty"}
    from file_browser import git_commit
    return git_commit(cwd, message)


def _rpc_git_commit_and_push(params: dict) -> dict:
    cwd = params.get("cwd") or ""
    message = params.get("message") or ""
    _validate_path(cwd)
    _assert_within_cwd_roots(cwd)
    if not message.strip():
        return {"ok": False, "error": "Commit message cannot be empty"}
    from file_browser import git_commit_and_push
    return git_commit_and_push(cwd, message)


def _rpc_scan_project_configs(params: dict) -> dict:
    cwd = params.get("cwd") or ""
    _validate_path(cwd)
    _assert_within_cwd_roots(cwd)
    from project_config import scan_project_configs
    return scan_project_configs(cwd)


def _rpc_file_editor_baseline(params: dict) -> dict:
    """One-shot read for `file_editor.start`'s session-create flow:
    validates the file + cwd exist, returns the file's resolved
    canonical path and current text content (used as the per-session
    `original_contents` baseline)."""
    file_path = params.get("file_path") or ""
    cwd = params.get("cwd") or ""
    _validate_path(file_path)
    _assert_within_cwd_roots(file_path)
    fp = Path(file_path).expanduser()
    if not fp.is_file():
        raise FileNotFoundError(f"file not found: {file_path}")
    if cwd:
        _validate_path(cwd)
        _assert_within_cwd_roots(cwd)
        cp = Path(cwd).expanduser()
        if not cp.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")
        cwd_resolved = str(cp.resolve())
    else:
        cwd_resolved = ""
    return {
        "file_path_resolved": str(fp.resolve()),
        "cwd_resolved": cwd_resolved,
        "original_content": fp.read_text(encoding="utf-8"),
    }


def _rpc_file_editor_project_cwd(params: dict) -> dict:
    cwd = params.get("cwd") or ""
    _validate_path(cwd)
    _assert_within_cwd_roots(cwd)
    cp = Path(cwd).expanduser()
    if not cp.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    return {"cwd_resolved": str(cp.resolve())}


def _rpc_list_sessions(params: dict) -> dict:
    """Scan local ba_home()/sessions/ for session summary files.

    Returns sessions with metadata (from standalone Better Agent usage
    on this node). Directories with only events.jsonl (spawn_run
    artifacts already tracked on primary) are skipped — they have no
    metadata and the primary already owns them.
    """
    import json as _json
    from paths import ba_home
    sessions_dir = ba_home() / "sessions"
    if not sessions_dir.is_dir():
        return {"sessions": []}
    result = []
    for f in sessions_dir.iterdir():
        if not f.is_file() or not f.name.endswith(".summary.json"):
            continue
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("id"):
            continue
        result.append({
            "id": data["id"],
            "name": data.get("name", ""),
            "cwd": data.get("cwd", ""),
            "model": data.get("model", ""),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "orchestration_mode": data.get("orchestration_mode"),
            "source": data.get("source"),
            "kind": data.get("kind"),
        })
    return {"sessions": result}


# Prompt-engineer temp files live under this node's own state home —
# NEVER under a client-supplied path. The eng_session_id is the only
# client input and is shape-validated, so the served path is confined
# to ba_home()/prompt-eng/ by construction.
_ENG_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _pe_temp_dir(eng_session_id) -> Path:
    if not isinstance(eng_session_id, str) or not _ENG_ID_RE.match(eng_session_id):
        raise ValueError(f"invalid eng_session_id: {eng_session_id!r}")
    from paths import ba_home
    return ba_home() / "prompt-eng" / eng_session_id


def _rpc_pe_temp_write(params: dict) -> dict:
    content = params.get("content")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    d = _pe_temp_dir(params.get("eng_session_id"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / "prompt.md"
    p.write_text(content, encoding="utf-8")
    return {"path": str(p)}


def _rpc_pe_temp_read(params: dict) -> dict:
    p = _pe_temp_dir(params.get("eng_session_id")) / "prompt.md"
    # Missing-file is signalled in-band: exception TYPES don't survive
    # the node WS (remote errors collapse to RuntimeError), and the
    # caller maps missing → 404.
    if not p.is_file():
        return {"content": None}
    return {"content": p.read_text(encoding="utf-8")}


def _rpc_pe_temp_cleanup(params: dict) -> dict:
    import shutil
    d = _pe_temp_dir(params.get("eng_session_id"))
    removed = d.exists()
    shutil.rmtree(d, ignore_errors=True)
    return {"removed": removed}


# Raw (binary) file serving for /api/file/raw on remote nodes. The WS
# frames are JSON, so chunks travel base64-encoded; 4MB raw (≈5.3MB
# encoded) stays far below the 64MB WS frame cap while keeping the
# number of round-trips per video/PDF small.
_RAW_RANGE_MAX = 4 * 1024 * 1024


def _rpc_get_raw_file_info(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    from file_browser import get_raw_file_info
    return get_raw_file_info(path_str)


def _rpc_read_file_raw_range(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    start = params.get("start")
    length = params.get("length")
    if not isinstance(start, int) or not isinstance(length, int):
        raise ValueError("start and length must be integers")
    if start < 0 or length <= 0:
        raise ValueError("start must be >= 0 and length > 0")
    if length > _RAW_RANGE_MAX:
        raise ValueError(f"length exceeds max chunk size {_RAW_RANGE_MAX}")
    import base64
    # Re-resolve through get_raw_file_info on EVERY read so the media
    # extension allowlist is enforced per-chunk, not just at info time.
    from file_browser import get_raw_file_info
    info = get_raw_file_info(path_str)
    with open(info["path"], "rb") as f:
        f.seek(start)
        data = f.read(length)
    return {"data_b64": base64.b64encode(data).decode("ascii")}


def _rpc_dir_exists(params: dict) -> dict:
    path_str = params.get("path") or ""
    _validate_path(path_str)
    _assert_within_cwd_roots(path_str)
    return {"is_dir": Path(path_str).is_dir()}


# Remote-run recovery RPCs. Paths are NEVER taken from the client:
# run_ids are shape-validated and resolved under this node's own runs
# root; the jsonl path comes from the run's own state.json.
def _read_run_json(run_dir: Path, name: str) -> Optional[dict]:
    import json as _json
    p = run_dir / name
    if not p.exists():
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _rpc_get_run_status(params: dict) -> dict:
    run_ids = params.get("run_ids")
    if not isinstance(run_ids, list) or len(run_ids) > 500:
        raise ValueError("run_ids must be a list (max 500)")
    from runs_dir import pid_alive as _pid_alive
    from runs_dir import runs_root
    out: dict[str, dict] = {}
    for rid in run_ids:
        if not isinstance(rid, str) or not _RUN_ID_RE.match(rid):
            raise ValueError(f"invalid run_id: {rid!r}")
        rd = runs_root() / rid
        if not rd.is_dir():
            out[rid] = {"exists": False}
            continue
        complete = _read_run_json(rd, "complete.json")
        state = _read_run_json(rd, "state.json") or {}
        pid: Optional[int] = None
        try:
            pid = int((rd / "pid").read_text().strip())
        except Exception:
            pass
        alive = bool(pid) and complete is None and _pid_alive(pid)
        out[rid] = {
            "exists": True,
            "alive": alive,
            "complete": complete,
            "session_id": state.get("session_id"),
            "jsonl_path": state.get("jsonl_path"),
            "pre_query_line_count": state.get("pre_query_line_count") or 0,
        }
    return {"runs": out}


# ── provider run ops (request/response, NOT streaming runs) ─────────
# These forward to the node's own provider so the claude→result
# translation stays single-path (no second copy of run_headless/rewind
# logic). They mirror spawn_run's trust model — the primary is trusted
# after approval, so cwd is not re-gated against cwd_roots, exactly
# like spawn_run. run_headless spawns a one-shot `claude -p`; rewind
# reverts a turn's file edits.
async def _rpc_run_headless(params: dict) -> dict:
    provider = default_provider()
    timeout = params.get("timeout")
    result = await provider.run_headless(
        prompt=params.get("prompt") or "",
        session_id=params.get("session_id"),
        resume_sid=params.get("resume_sid"),
        fork=bool(params.get("fork")),
        cwd=params.get("cwd"),
        timeout=timeout if isinstance(timeout, (int, float)) else None,
    )
    return {"result": result}


async def _rpc_rewind(params: dict) -> dict:
    provider = default_provider()
    agent_sid = params.get("agent_sid") or ""
    message_uuid = params.get("message_uuid") or ""
    if not agent_sid or not message_uuid:
        raise ValueError("rewind requires agent_sid and message_uuid")
    await provider.rewind(agent_sid, message_uuid)
    return {"ok": True}


_RUN_JSONL_BYTE_BUDGET = 4 * 1024 * 1024


def _rpc_read_run_jsonl(params: dict) -> dict:
    """Page through a run's claude session jsonl (path taken from the
    run's OWN state.json, never the client). Primary uses this to
    rebuild the shadow jsonl before replaying a recovered remote run."""
    rid = params.get("run_id")
    if not isinstance(rid, str) or not _RUN_ID_RE.match(rid):
        raise ValueError(f"invalid run_id: {rid!r}")
    start_line = params.get("start_line")
    if not isinstance(start_line, int) or start_line < 0:
        raise ValueError("start_line must be a non-negative integer")
    from runs_dir import runs_root
    rd = runs_root() / rid
    state = _read_run_json(rd, "state.json") or {}
    jsonl_path = state.get("jsonl_path")
    if not jsonl_path or not Path(jsonl_path).is_file():
        return {"lines": [], "next_line": start_line, "eof": True}
    lines: list[str] = []
    budget = _RUN_JSONL_BYTE_BUDGET
    eof = True
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f):
            if i < start_line:
                continue
            if budget <= 0:
                eof = False
                break
            line = raw.rstrip("\n")
            lines.append(line)
            budget -= len(line)
    return {"lines": lines, "next_line": start_line + len(lines), "eof": eof}


_HANDLERS = {
    "list_dir": _rpc_list_dir,
    "list_sessions": _rpc_list_sessions,
    "list_directories": _rpc_list_directories,
    "get_file_tree": _rpc_get_file_tree,
    "search_tree": _rpc_search_tree,
    "get_file_content": _rpc_get_file_content,
    "get_file_metadata": _rpc_get_file_metadata,
    "write_file_content": _rpc_write_file_content,
    "create_file": _rpc_create_file,
    "create_directory": _rpc_create_directory,
    "reconstruct_before_edit": _rpc_reconstruct_before_edit,
    "get_git_status": _rpc_get_git_status,
    "get_file_diff": _rpc_get_file_diff,
    "git_commit": _rpc_git_commit,
    "git_commit_and_push": _rpc_git_commit_and_push,
    "scan_project_configs": _rpc_scan_project_configs,
    "file_editor_baseline": _rpc_file_editor_baseline,
    "file_editor_project_cwd": _rpc_file_editor_project_cwd,
    "dir_exists": _rpc_dir_exists,
    "get_run_status": _rpc_get_run_status,
    "read_run_jsonl": _rpc_read_run_jsonl,
    "get_raw_file_info": _rpc_get_raw_file_info,
    "read_file_raw_range": _rpc_read_file_raw_range,
    "pe_temp_write": _rpc_pe_temp_write,
    "pe_temp_read": _rpc_pe_temp_read,
    "pe_temp_cleanup": _rpc_pe_temp_cleanup,
    "run_headless": _rpc_run_headless,
    "rewind": _rpc_rewind,
}


def _local_node_id() -> str:
    try:
        from topology import local_node_id as _lid
        return _lid()
    except Exception:
        return get_env("BETTER_CLAUDE_NODE_ID") or "primary"
