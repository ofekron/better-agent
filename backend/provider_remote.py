"""RemoteProviderProxy — Provider impl that proxies start_run/cancel_run
to a worker-node over the persistent node_link WS.

PURE TRANSPORT (single-code-path invariant): this class deserializes
incoming `event_forward` / `run_control` messages from the node and
re-pushes the resulting `StreamEvent` onto a local `asyncio.Queue` —
exactly like `ClaudeProvider`'s `_bootstrap_run` would. The actual
claude→StreamEvent translation logic lives on the NODE's
`ClaudeProvider`. There is no second copy of that logic here.

Crash recovery: every remote run gets a primary-side run dir
(`runs/<run_id>/backend_state.json` stamped with `node_id`) so a
primary restart knows which runs were in flight on which node. The
dir gains `complete.json` (+ `reconciled.marker` when the terminal
arrived on a live, orchestrator-owned run) from `_on_run_control`.
Unreconciled dirs are integrated by
`run_recovery.integrate_remote_runs_for_node` when the node
(re)connects — see that module for the replay/rehook flow.

`run_headless` and `rewind` are request/response RPCs over
`node_link.rpc_call` — they forward to the node's own provider, so the
claude→result translation stays single-path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import node_link
import node_store
import perf
import config_store
from provider import Provider, StreamEvent
from reasoning_effort import CLAUDE_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT
from runs_dir import atomic_write_json, runs_root

logger = logging.getLogger(__name__)


@dataclass
class _RemoteRunState:
    """Mirror of ClaudeProvider.RunState that the proxy needs to track.

    `popen` is set to a sentinel so the base Provider methods that
    poll process state behave predictably (always alive until we mark
    cancelled or complete).

    `loop` is captured at start_run time so cancel_run can schedule
    its WS send onto the right event loop without relying on a fragile
    `asyncio.get_event_loop()` lookup."""
    run_id: str
    run_dir: Path
    mode: str
    app_session_id: str
    queue: asyncio.Queue
    node_id: str
    loop: asyncio.AbstractEventLoop
    cancelled: bool = False
    finished: bool = False
    session_id: Optional[str] = None
    started_at: str = ""
    persist_to: Optional[str] = None
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None


class _FakePopen:
    """Stand-in so base-class methods that poke `rs.popen.poll()` don't
    crash. A remote run is "alive" while not finished."""
    def __init__(self, state: "_RemoteRunState") -> None:
        self._state = state
        self.pid = -1

    def poll(self) -> Optional[int]:
        return 0 if self._state.finished else None

    def wait(self, timeout: Optional[float] = None) -> int:  # noqa: ARG002
        return 0


class RemoteProviderProxy(Provider):
    """One instance per worker-node. Held by the coordinator and
    returned from `provider_for_session` whenever the session's
    `node_id` does not match the local node."""

    KIND = "claude-remote"
    supports_reasoning_effort = True
    reasoning_effort_options = CLAUDE_REASONING_EFFORTS
    default_reasoning_effort = DEFAULT_REASONING_EFFORT
    # ASSUMPTION (v1): a worker-node always runs the Claude provider —
    # the KIND name encodes this. Capability flags inherit Provider's
    # defaults (`supports_fork=True`, `supports_manager_mode=True`,
    # `supports_rewind=True`), matching ClaudeProvider. If a future
    # node hosts a Gemini provider, the proxy MUST forward the remote
    # provider's actual capability flags via the node WS at startup;
    # today's inherited defaults would lie. Gate any new node-side
    # provider kind on that plumbing.

    def __init__(self, node_id: str) -> None:
        # Synthesize a minimal record so Provider.__init__ accepts us.
        super().__init__({"id": f"remote:{node_id}", "kind": self.KIND})
        self.node_id = node_id
        self._runs: dict[str, _RemoteRunState] = {}
        self._lock = threading.Lock()
        # Aggregate gauge name stashed on the instance so
        # `provider.get_provider` can unregister it when the provider
        # record is deleted AND re-register on resurrection.
        self._perf_gauge_name = f"provider.remote.{node_id}.run_q"
        self._register_perf_gauge()

    def _register_perf_gauge(self) -> None:
        perf.register_queue(
            self._perf_gauge_name,
            lambda: sum(rs.queue.qsize() for rs in self._runs.values()),
        )

    # ------------------------------------------------------------------
    # start_run — ship spawn_run over WS, register local proxy state.
    # ------------------------------------------------------------------
    def start_run(
        self,
        *,
        run_id: str,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        model: Optional[str],
        reasoning_effort: Optional[str],
        session_id: Optional[str],
        mode: str,
        app_session_id: str,
        source: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        setting_sources: Optional[list[str]] = None,
        backend_url: Optional[str] = None,
        internal_token: Optional[str] = None,
        fork: bool = False,
        supervised: bool = False,
        supervisor_agent_session_id: Optional[str] = None,
        worker_agent_session_id: Optional[str] = None,
        mssg_sender_session_id: Optional[str] = None,
        is_worker: bool = False,
        browser_harness_enabled: bool = False,
        open_file_panel_enabled: bool = False,
        working_mode: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        continuation_chain: Optional[list[str]] = None,
        provider_run_config: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        target_message_id: Optional[str] = None,
        turn_run_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
    ) -> None:
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        # Layer-3 capability defense (matches GeminiProvider.start_run).
        # `supports_manager_mode` + `supports_fork` reflect the v1
        # assumption above (remote = Claude only → all True). If a
        # future remote node ever runs a non-Claude provider, the
        # spawn_run RPC must forward the actual provider's flags here
        # before the run starts — otherwise these guards are vacuous.
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError(
                f"{self.KIND} provider does not support team mode."
            )
        if fork and not self.supports_fork:
            raise NotImplementedError(
                f"{self.KIND} provider does not support fork."
            )
        if images:
            raise NotImplementedError(
                "remote workers: image inputs are not supported in v1"
            )

        # Resolve the session's root_id so the node can ingest into the
        # right events.jsonl directory.
        from session_manager import manager as session_manager
        root_id = session_manager._root_id_for(
            worker_agent_session_id or app_session_id
        )
        if root_id is None:
            raise RuntimeError(
                f"RemoteProviderProxy.start_run: no root_id for "
                f"agent_session_id={worker_agent_session_id or app_session_id!r}"
            )

        # Primary-side run dir: the durable record that THIS run was in
        # flight on THIS node, so a primary restart can reconcile it via
        # `run_recovery.integrate_remote_runs_for_node` once the node
        # reconnects. Events/jsonl stay on the node (and in the shadow);
        # only the descriptor lives here.
        run_dir = runs_root() / run_id
        started_at = datetime.now().isoformat()
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(run_dir / "backend_state.json", {
                "provider_id": self.id,
                "node_id": self.node_id,
                "root_id": root_id,
                "app_session_id": app_session_id,
                "persist_to": worker_agent_session_id or app_session_id,
                "mode": mode,
                "source": source or "",
                "session_id": session_id,
                "cwd": cwd,
                "started_at": started_at,
                "target_message_id": target_message_id,
                "turn_run_id": turn_run_id,
            })
        except Exception:
            logger.exception(
                "RemoteProviderProxy: failed to persist run dir for %s", run_id,
            )

        state = _RemoteRunState(
            run_id=run_id,
            run_dir=run_dir,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            node_id=self.node_id,
            loop=loop,
            session_id=session_id,
            started_at=started_at,
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        # popen is queried by base class methods like is_running.
        state.popen = _FakePopen(state)  # type: ignore[attr-defined]
        with self._lock:
            self._runs[run_id] = state

        # Track in node_store so inbound messages can find this run.
        conn = node_store.get_connection(self.node_id)
        if conn is None:
            raise node_link.NodeOffline(
                f"node {self.node_id!r} is offline; cannot start run"
            )
        conn.runs[run_id] = state

        # Provider-native config stays local to the executing node's CLI.
        # The coordinator's local config paths do not apply to remote runs.
        payload = {
            "run_id": run_id,
            "prompt": prompt,
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "session_id": session_id,
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "worker_agent_session_id": worker_agent_session_id,
            "mssg_sender_session_id": mssg_sender_session_id,
            "root_id": root_id,
            "fork": fork,
            "setting_sources": setting_sources,
            "disallowed_tools": disallowed_tools,
            "backend_url": backend_url,
            "internal_token": internal_token,
            "supervised": supervised,
            "supervisor_agent_session_id": supervisor_agent_session_id,
            "is_worker": is_worker,
            "browser_harness_enabled": browser_harness_enabled,
            "open_file_panel_enabled": open_file_panel_enabled,
            "working_mode": working_mode,
            "extra_env": extra_env,
            "files": files,
            "continuation_chain": continuation_chain or [],
            "provider_run_config": provider_run_config or {},
            "capability_contexts": capability_contexts or [],
            "target_message_id": target_message_id,
            "turn_run_id": turn_run_id,
            "disabled_builtin_extensions": (
                disabled_builtin_extensions
                if disabled_builtin_extensions is not None
                else config_store.get_disabled_builtin_extensions()
            ),
        }
        # spawn_run send is async. If it raises (node disconnected
        # between the get_connection check and the actual ws.send), we
        # MUST enqueue an `error` StreamEvent so the caller's queue.get()
        # drain loop doesn't hang forever. Done via a wrapper task,
        # not fire-and-forget.
        async def _send_or_fail() -> None:
            try:
                await node_link.send_spawn_run(self.node_id, payload)
            except Exception as e:
                logger.exception(
                    "RemoteProviderProxy: send_spawn_run failed run=%s", run_id,
                )
                try:
                    queue.put_nowait(StreamEvent(
                        type="error",
                        data={"error": f"remote spawn failed: {type(e).__name__}: {e}"},
                    ))
                except Exception:
                    pass
                state.finished = True
                self._runs.pop(run_id, None)
                conn2 = node_store.get_connection(self.node_id)
                if conn2:
                    conn2.runs.pop(run_id, None)
                # The spawn never reached the node and the live queue
                # got the error — finalize the run dir so recovery
                # never tries to reconcile a run that never ran.
                _finalize_remote_run_dir(
                    run_dir,
                    {"success": False, "session_id": session_id,
                     "error": f"remote spawn failed: {type(e).__name__}: {e}",
                     "token_usage": None,
                     "finished_at": datetime.now().isoformat()},
                    reconciled=True,
                )
        asyncio.run_coroutine_threadsafe(_send_or_fail(), loop)

    # ------------------------------------------------------------------
    # cancel_run — ship cancel_run over WS; mark state cancelled. The
    # node's drain will emit a final complete/error which clears the
    # local state in _on_run_control.
    # ------------------------------------------------------------------
    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            rs = self._runs.get(run_id)
        if rs is None:
            return False
        rs.cancelled = True
        # Schedule onto the loop we captured at start_run — calling
        # asyncio.get_event_loop() from a sync context is deprecated
        # in Py 3.12+ and unreliable when cancel_run runs from a
        # worker thread (e.g. signal-handler cancel_all).
        try:
            asyncio.run_coroutine_threadsafe(
                node_link.send_cancel_run(self.node_id, run_id), rs.loop,
            )
        except Exception:
            logger.exception(
                "RemoteProviderProxy.cancel_run: send failed run=%s", run_id,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Stub the rest of the Provider ABC for v1.
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        # Never called for remote — the env is built on the node side.
        return {}

    def _write_backend_state(self, rs: Any) -> None:
        return None

    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        # Remote recovery cannot run at startup scan time — it needs
        # the node online to classify runs. It is driven instead by
        # `run_recovery.integrate_remote_runs_for_node`, fired from the
        # node_store "connected" transition listener (wired in main.py).
        # Startup's `recover_all_in_flight` intentionally skips dirs
        # owned by `remote:*` provider ids.
        return []

    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return 0

    async def run_headless(
        self,
        *,
        prompt: str,
        session_id: Optional[str] = None,
        resume_sid: Optional[str] = None,
        fork: bool = False,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        no_tools: bool = False,
    ) -> Optional[dict]:
        # One-shot request/response RPC: the node's own provider runs
        # `claude -p` and returns the JSON envelope. The CLI is bounded
        # by its own `timeout`; the WS round-trip gets a generous
        # ceiling so a long turn isn't cut by rpc_call's 30s default.
        rpc_timeout = (
            timeout + 30 if isinstance(timeout, (int, float)) else 1800.0
        )
        resp = await node_link.rpc_call(
            self.node_id, "run_headless",
            {
                "prompt": prompt,
                "session_id": session_id,
                "resume_sid": resume_sid,
                "fork": fork,
                "cwd": cwd,
                "timeout": timeout,
                "no_tools": no_tools,
            },
            timeout=rpc_timeout,
        )
        if not isinstance(resp, dict):
            return None
        return resp.get("result")

    async def rewind(self, agent_sid: str, message_uuid: str) -> None:
        # ClaudeProvider.rewind raises RuntimeError on non-zero CLI exit;
        # rpc_call re-raises that from the node's error reply, so the
        # caller sees the same exception shape as a local rewind.
        await node_link.rpc_call(
            self.node_id, "rewind",
            {"agent_sid": agent_sid, "message_uuid": message_uuid},
            timeout=120.0,
        )

    @property
    def models(self) -> list[str]:
        return []


# ============================================================================
# Inbound dispatchers — wired into node_link at module import.
# Provider lookup: per-node singleton, lazily created.
# ============================================================================
_proxies: dict[str, RemoteProviderProxy] = {}
_proxies_lock = threading.Lock()


def get_proxy(node_id: str) -> RemoteProviderProxy:
    with _proxies_lock:
        proxy = _proxies.get(node_id)
        if proxy is None:
            proxy = RemoteProviderProxy(node_id)
            _proxies[node_id] = proxy
        return proxy


async def _on_event_forward(
    *,
    node_id: str,
    run_id: str,
    event_type: str,
    data: dict,
) -> None:
    """node_link calls this for every inbound event_forward. We push a
    StreamEvent onto the matching run's queue exactly like the local
    provider's bootstrap task would — this is the seam where remote and
    local converge on the SAME consumer code path."""
    if run_id is None:
        return
    proxy = _proxies.get(node_id)
    if proxy is None:
        return
    rs = proxy._runs.get(run_id)
    if rs is None:
        # No live drain loop (e.g. primary restarted mid-turn). The
        # event was ALREADY journaled by node_link._handle_event_forward
        # before this dispatcher ran, so nothing is lost — only the
        # per-turn streaming consumer is absent.
        logger.debug(
            "provider_remote: event_forward %s for unknown run %s",
            event_type, run_id,
        )
        return
    try:
        rs.queue.put_nowait(StreamEvent(type=event_type, data=data))
    except asyncio.QueueFull:
        logger.warning(
            "remote provider queue full for run=%s; dropping event_type=%s",
            run_id, event_type,
        )


def _finalize_remote_run_dir(
    run_dir: Path, complete_payload: dict, *, reconciled: bool,
) -> None:
    """Write `complete.json` (and optionally `reconciled.marker`) into a
    primary-side remote run dir. `reconciled=True` is correct ONLY when
    the terminal was consumed by a live drain loop — the live mirror
    already persisted everything, so recovery must never replay it."""
    try:
        if not run_dir.is_dir():
            return
        if not (run_dir / "complete.json").exists():
            atomic_write_json(run_dir / "complete.json", complete_payload)
        if reconciled:
            (run_dir / "reconciled.marker").touch(exist_ok=True)
    except Exception:
        logger.exception(
            "provider_remote: failed to finalize run dir %s", run_dir,
        )


def _complete_payload(control_type: str, data: dict) -> dict:
    return {
        "success": bool(data.get("success", control_type == "complete")),
        "session_id": data.get("session_id"),
        "error": data.get("error"),
        "token_usage": data.get("token_usage"),
        "finished_at": datetime.now().isoformat(),
    }


async def _on_run_control(
    *,
    node_id: str,
    run_id: str,
    control_type: str,
    data: dict,
) -> None:
    """node_link calls this for every inbound run_control. session_discovered/
    complete/error get pushed onto the run's queue so the local
    _delegation/_subprocess_agent drain loops see them exactly like
    local runs.

    Terminal controls also finalize the primary-side run dir:
    - run known (live drain loop consumes it) → complete.json +
      reconciled.marker (live mirror persisted everything).
    - run unknown (primary restarted; the node re-shipped after a
      rehook) → complete.json only, then schedule integration so the
      session's message is finalized from the shadow replay.
    """
    if run_id is None or control_type is None:
        return
    # get_proxy (NOT _proxies.get): after a primary restart no proxy
    # exists until a turn runs, and a terminal for a recovering run
    # must still reach the unknown-run handling below — otherwise the
    # first completion after a restart is dropped until the node's
    # next reconnect.
    proxy = get_proxy(node_id)
    rs = proxy._runs.get(run_id)
    if rs is None:
        if control_type in ("complete", "error"):
            run_dir = runs_root() / run_id
            if run_dir.is_dir() and not (run_dir / "reconciled.marker").exists():
                _finalize_remote_run_dir(
                    run_dir, _complete_payload(control_type, data),
                    reconciled=False,
                )
                try:
                    import run_recovery
                    asyncio.get_running_loop().create_task(
                        run_recovery.integrate_remote_runs_for_node(
                            node_id, run_id_filter={run_id},
                        )
                    )
                except Exception:
                    logger.exception(
                        "provider_remote: failed to schedule integration "
                        "for rehooked run %s", run_id,
                    )
            else:
                logger.debug(
                    "provider_remote: terminal %s for unknown run %s "
                    "(no pending run dir)", control_type, run_id,
                )
        return
    try:
        rs.queue.put_nowait(StreamEvent(type=control_type, data=data))
    except asyncio.QueueFull:
        logger.warning(
            "remote provider control queue full for run=%s; dropping %s",
            run_id, control_type,
        )
    if control_type in ("complete", "error"):
        rs.finished = True
        proxy._runs.pop(run_id, None)
        conn = node_store.get_connection(node_id)
        if conn:
            conn.runs.pop(run_id, None)
        _finalize_remote_run_dir(
            rs.run_dir, _complete_payload(control_type, data),
            reconciled=True,
        )


# Wire dispatchers into node_link as soon as this module is imported.
node_link.set_dispatchers(
    run_control=_on_run_control,
    event_forward=_on_event_forward,
)
