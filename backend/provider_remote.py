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
import concurrent.futures
import logging
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import node_link
import node_store
import perf
import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
from provider import Provider, StreamEvent
from provider_lifecycle import LifecycleOutcome, RunLifecycleCoordinator
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
    lifecycle_msg_id: Optional[str] = None
    lifecycle_token: Any = None
    lifecycle_record: Any = None
    cancel_sent: bool = False
    terminal_delivered: bool = False
    lifecycle_nonce: str = ""
    spawn_sent: bool = False


@dataclass(frozen=True, slots=True)
class RemoteLifecycleRecord:
    run_id: str
    cleanup_nonce: str
    node_id: str
    run_dir: str


class RemoteStartRejected(RuntimeError):
    """The node durably rejected the nonce generation before acceptance."""


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
        self._lifecycle: RunLifecycleCoordinator[RemoteLifecycleRecord] | None = None
        self._lifecycle_tasks: set[Any] = set()
        self._pending_states: dict[str, _RemoteRunState] = {}
        self._pending_acks: dict[str, asyncio.Future] = {}
        self._pending_nonces: dict[str, str] = {}
        self._lifecycle_runs: dict[str, _RemoteRunState] = {}
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
        lifecycle_msg_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
    ) -> None:
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        self.assert_not_suspended(action="start new runs")
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
        session_record = session_manager.get(app_session_id) or {}
        worker_record = (
            session_manager.get(worker_agent_session_id)
            if worker_agent_session_id
            else {}
        )
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
            lifecycle_msg_id=lifecycle_msg_id,
            lifecycle_nonce=uuid.uuid4().hex,
        )
        # popen is queried by base class methods like is_running.
        state.popen = _FakePopen(state)  # type: ignore[attr-defined]
        conn = node_store.get_connection(self.node_id)
        if conn is None:
            raise node_link.NodeOffline(
                f"node {self.node_id!r} is offline; cannot start run"
            )

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
            "lifecycle_msg_id": lifecycle_msg_id,
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=session_record,
                    worker_record=worker_record,
                )
            ),
        }
        # spawn_run send is async. If it raises (node disconnected
        # between the get_connection check and the actual ws.send), we
        # MUST enqueue an `error` StreamEvent so the caller's queue.get()
        # drain loop doesn't hang forever. Done via a wrapper task,
        # not fire-and-forget.
        if self._lifecycle is None:
            self._lifecycle = RunLifecycleCoordinator(loop)
        initial_state = {
            "provider_id": self.id, "provider_kind": self.KIND,
            "node_id": self.node_id, "root_id": root_id,
            "app_session_id": state.app_session_id,
            "persist_to": state.persist_to, "mode": state.mode,
            "source": source or "", "session_id": state.session_id,
            "cwd": cwd, "started_at": state.started_at,
            "target_message_id": state.target_message_id,
            "turn_run_id": state.turn_run_id,
            "lifecycle_msg_id": state.lifecycle_msg_id, "run_id": state.run_id,
            "lifecycle_nonce": state.lifecycle_nonce,
            "lifecycle_generation": 0,
            "lifecycle_state": "reserved",
        }
        state.run_dir.mkdir(parents=True, exist_ok=True)
        backend_state_path = state.run_dir / "backend_state.json"
        atomic_write_json(backend_state_path, initial_state)
        import active_run_catalog
        active_run_catalog.register(backend_state_path, initial_state)
        self._pending_states[state.run_id] = state
        self._pending_nonces[state.run_id] = state.lifecycle_nonce
        task = asyncio.run_coroutine_threadsafe(
            self._admit_send_publish(state, payload, root_id=root_id, cwd=cwd, source=source),
            loop,
        )
        self._lifecycle_tasks.add(task)
        self._track_run_start_receipt(run_id, task)
        task.add_done_callback(self._consume_lifecycle_task)

    def _consume_lifecycle_task(self, task: Any) -> None:
        self._lifecycle_tasks.discard(task)
        try:
            task.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            pass
        except BaseException:
            logger.debug("remote lifecycle task failed", exc_info=True)

    async def _admit_send_publish(
        self, state: _RemoteRunState, payload: dict, *, root_id: str,
        cwd: str, source: Optional[str],
    ) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("remote lifecycle coordinator unavailable")
        admission = await lifecycle.admit(state.run_id, nonce=state.lifecycle_nonce)
        if not admission.accepted or admission.token is None:
            raise RuntimeError(f"remote run admission rejected: {admission.outcome.value}")
        token = admission.token
        ack = asyncio.get_running_loop().create_future()
        self._pending_states.setdefault(state.run_id, state)
        self._pending_nonces.setdefault(state.run_id, token.nonce)
        self._pending_acks[state.run_id] = ack
        sent = False
        backend_state = {
            "provider_id": self.id, "provider_kind": self.KIND,
            "node_id": self.node_id, "root_id": root_id,
            "app_session_id": state.app_session_id,
            "persist_to": state.persist_to, "mode": state.mode,
            "source": source or "", "session_id": state.session_id,
            "cwd": cwd, "started_at": state.started_at,
            "target_message_id": state.target_message_id,
            "turn_run_id": state.turn_run_id,
            "lifecycle_msg_id": state.lifecycle_msg_id, "run_id": state.run_id,
            "lifecycle_nonce": token.nonce,
            "lifecycle_generation": token.generation,
            "lifecycle_state": "cancelling" if state.cancelled else "pending",
        }
        try:
            state.run_dir.mkdir(parents=True, exist_ok=True)
            backend_state_path = state.run_dir / "backend_state.json"
            atomic_write_json(backend_state_path, backend_state)
            import active_run_catalog
            active_run_catalog.register(backend_state_path, backend_state)
            request_payload = dict(payload)
            request_payload["lifecycle_nonce"] = token.nonce
            await node_link.send_spawn_run(self.node_id, request_payload)
            sent = True
            state.spawn_sent = True
            if state.cancelled:
                await self._send_cancel_once(state)
            await asyncio.wait_for(asyncio.shield(ack), timeout=30.0)
            if self._pending_states.get(state.run_id) is not state:
                raise asyncio.CancelledError()
            backend_state["lifecycle_state"] = (
                "cancelling" if state.cancelled else "accepted"
            )
            atomic_write_json(backend_state_path, backend_state)
            active_run_catalog.register(backend_state_path, backend_state)
            record = RemoteLifecycleRecord(
                state.run_id, uuid.uuid4().hex, self.node_id, str(state.run_dir)
            )
            published = await lifecycle.publish(token, record)
            if not published.accepted:
                raise RuntimeError(f"remote run publish rejected: {published.outcome.value}")
            state.lifecycle_token = token
            state.lifecycle_record = record
            self._lifecycle_runs[record.cleanup_nonce] = state
            self._publish_started_run(state.run_id, state)
            conn = node_store.get_connection(self.node_id)
            if conn is not None:
                conn.runs[state.run_id] = state
        except BaseException as exc:
            terminal_rejection = isinstance(exc, RemoteStartRejected)
            if sent:
                if not terminal_rejection:
                    backend_state["lifecycle_state"] = "cancelling"
                    try:
                        atomic_write_json(backend_state_path, backend_state)
                        active_run_catalog.register(backend_state_path, backend_state)
                    except BaseException:
                        logger.exception(
                            "remote uncertain-start cancellation persist failed run=%s",
                            state.run_id,
                        )
                await self._send_cancel_once(state)
            if not sent or terminal_rejection:
                try:
                    import active_run_catalog
                    active_run_catalog.retire(runs_root(), state.run_id)
                except BaseException:
                    logger.exception("remote rollback catalog cleanup failed run=%s", state.run_id)
                try:
                    shutil.rmtree(state.run_dir)
                except FileNotFoundError:
                    pass
                except BaseException:
                    logger.exception("remote rollback descriptor cleanup failed run=%s", state.run_id)
            try:
                await lifecycle.rollback(token)
            except BaseException:
                logger.exception("remote rollback reservation cleanup failed run=%s", state.run_id)
            try:
                state.queue.put_nowait(StreamEvent(
                    "error", {"error": f"remote spawn failed: {type(exc).__name__}: {exc}"}
                ))
            except Exception:
                pass
            raise
        finally:
            self._pending_states.pop(state.run_id, None)
            self._pending_acks.pop(state.run_id, None)
            self._pending_nonces.pop(state.run_id, None)

    async def _send_cancel_once(self, state: _RemoteRunState) -> bool:
        if not state.spawn_sent:
            return False
        if state.cancel_sent:
            return False
        state.cancel_sent = True
        try:
            nonce = getattr(getattr(state, "lifecycle_token", None), "nonce", None)
            if nonce is None:
                nonce = self._pending_nonces.get(state.run_id)
            result = bool(await node_link.send_cancel_run(
                self.node_id, state.run_id, lifecycle_nonce=nonce,
            ))
            if not result:
                state.cancel_sent = False
            return result
        except BaseException:
            state.cancel_sent = False
            logger.exception("remote cancel failed run=%s", state.run_id)
            return False

    # ------------------------------------------------------------------
    # cancel_run — ship cancel_run over WS; mark state cancelled. The
    # node's drain will emit a final complete/error which clears the
    # local state in _on_run_control.
    # ------------------------------------------------------------------
    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            rs = self._runs.get(run_id)
        if rs is None:
            rs = self._pending_states.get(run_id)
        if rs is None:
            return False
        rs.cancelled = True
        # Schedule onto the loop we captured at start_run — calling
        # asyncio.get_event_loop() from a sync context is deprecated
        # in Py 3.12+ and unreliable when cancel_run runs from a
        # worker thread (e.g. signal-handler cancel_all).
        try:
            asyncio.run_coroutine_threadsafe(self._cancel_owned(run_id), rs.loop)
        except Exception:
            logger.exception(
                "RemoteProviderProxy.cancel_run: send failed run=%s", run_id,
            )
            return False
        return True

    async def _cancel_owned(self, run_id: str) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return
        pending = self._pending_states.get(run_id)
        if pending is not None:
            try:
                import json
                backend_path = pending.run_dir / "backend_state.json"
                backend_state = json.loads(backend_path.read_text(encoding="utf-8"))
                backend_state["lifecycle_state"] = "cancelling"
                atomic_write_json(backend_path, backend_state)
                import active_run_catalog
                active_run_catalog.register(backend_path, backend_state)
            except Exception:
                logger.exception("remote pending cancel persist failed run=%s", run_id)
            await self._send_cancel_once(pending)
            return
        result = await lifecycle.cancel(run_id)
        state = self._pending_states.pop(run_id, None)
        if result.value is not None:
            state = self._lifecycle_runs.pop(result.value.cleanup_nonce, state)
        if state is not None:
            if state.run_dir.exists():
                try:
                    import json
                    backend_path = state.run_dir / "backend_state.json"
                    backend_state = json.loads(backend_path.read_text(encoding="utf-8"))
                    backend_state["lifecycle_state"] = "cancelling"
                    atomic_write_json(backend_path, backend_state)
                    import active_run_catalog
                    active_run_catalog.register(backend_path, backend_state)
                except Exception:
                    logger.exception("remote cancel state persist failed run=%s", run_id)
            await self._send_cancel_once(state)

    async def shutdown_lifecycle(self, *, terminate_runs: bool = True) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return
        await lifecycle.quiesce()
        if not terminate_runs:
            pending = tuple(self._lifecycle_tasks)
            if pending:
                await asyncio.gather(
                    *(asyncio.wrap_future(task) for task in pending),
                    return_exceptions=True,
                )
            await lifecycle.shutdown()
            return
        run_ids = tuple(set(self._pending_states) | set(self._runs))
        await asyncio.gather(*(self._cancel_owned(run_id) for run_id in run_ids))
        pending = tuple(self._lifecycle_tasks)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(task) for task in pending),
                return_exceptions=True,
            )
        await lifecycle.shutdown()

    # ------------------------------------------------------------------
    # Stub the rest of the Provider ABC for v1.
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        # Never called for remote — the env is built on the node side.
        return {}

    def _persists_backend_state(self, rs: Any) -> bool:
        return False

    def _backend_state_fields(self, rs: Any) -> dict[str, Any]:
        return {}

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
        self.assert_not_suspended(action="run headless work")
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
            version_ready_required=True,
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
    nonce = data.get("lifecycle_nonce") if isinstance(data, dict) else None
    if control_type == "accepted":
        ack = proxy._pending_acks.get(run_id)
        if (
            ack is not None
            and not ack.done()
            and isinstance(nonce, str) and bool(nonce)
            and nonce == proxy._pending_nonces.get(run_id)
        ):
            ack.set_result(data)
        return
    if control_type == "error":
        ack = proxy._pending_acks.get(run_id)
        if (
            ack is not None
            and not ack.done()
            and isinstance(data, dict)
            and isinstance(data.get("error"), str)
            and isinstance(nonce, str) and bool(nonce)
            and nonce == proxy._pending_nonces.get(run_id)
        ):
            ack.set_exception(RemoteStartRejected(data.get("error") or "remote start rejected"))
            return
    rs = proxy._runs.get(run_id)
    expected_nonce = getattr(getattr(rs, "lifecycle_token", None), "nonce", None)
    if rs is not None and (not isinstance(nonce, str) or not nonce or nonce != expected_nonce):
        logger.warning("provider_remote: rejected %s with missing/stale nonce run=%s", control_type, run_id)
        return
    if rs is None:
        if control_type in ("complete", "error"):
            run_dir = runs_root() / run_id
            if run_dir.is_dir() and not (run_dir / "reconciled.marker").exists():
                try:
                    import json
                    backend_state = json.loads(
                        (run_dir / "backend_state.json").read_text(encoding="utf-8")
                    )
                except Exception:
                    logger.warning("provider_remote: terminal for unreadable descriptor run=%s", run_id)
                    return
                if nonce != backend_state.get("lifecycle_nonce"):
                    logger.warning("provider_remote: rejected recovered terminal stale nonce run=%s", run_id)
                    return
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
        if not rs.terminal_delivered:
            rs.queue.put_nowait(StreamEvent(type=control_type, data=data))
            if control_type in ("complete", "error"):
                rs.terminal_delivered = True
    except asyncio.QueueFull:
        logger.warning(
            "remote provider control queue full for run=%s; dropping %s",
            run_id, control_type,
        )
    if control_type in ("complete", "error"):
        rs.finished = True
        proxy._runs.pop(run_id, None)
        record = rs.lifecycle_record
        token = rs.lifecycle_token
        if record is not None:
            proxy._lifecycle_runs.pop(record.cleanup_nonce, None)
        if token is not None and record is not None and proxy._lifecycle is not None:
            await proxy._lifecycle.retire(token, record)
        conn = node_store.get_connection(node_id)
        if conn:
            conn.runs.pop(run_id, None)
        _finalize_remote_run_dir(
            rs.run_dir, _complete_payload(control_type, data),
            reconciled=True,
        )
        try:
            import active_run_catalog
            active_run_catalog.retire(runs_root(), run_id)
        except Exception:
            logger.exception("provider_remote: catalog retire failed run=%s", run_id)


async def _on_node_state(node_id: str, state: str) -> None:
    proxy = _proxies.get(node_id)
    if state == "disconnected":
        if proxy is None:
            return
        error = node_link.NodeOffline(f"node {node_id!r} disconnected before run acceptance")
        for ack in tuple(proxy._pending_acks.values()):
            if not ack.done():
                ack.set_exception(error)
        return


# Wire dispatchers into node_link as soon as this module is imported.
node_link.set_dispatchers(
    run_control=_on_run_control,
    event_forward=_on_event_forward,
)
node_store.add_listener(_on_node_state)
