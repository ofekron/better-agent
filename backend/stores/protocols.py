"""A12: Per-bounded-context `Storage` Protocols.

Seven structural-typing declarations covering the seven persistence
bounded contexts in this backend:

  - `SessionsStorage`   — `session_store`
  - `WorkersStorage`    — `stores/worker_store`
  - `ApprovalsStorage`  — `stores/pending_approvals`
  - `NodesStorage`      — `node_store`
  - `ProjectsStorage`   — `project_store`
  - `ConfigStorage`     — `config_store`
  - `TracesStorage`     — `trace_collector`

INVARIANT — ba_home() called exactly once per port impl.
Each store module owns ONE root-resolver helper (e.g.
`session_store._sessions_dir`, `_traces_dir`, `_workers_dir`,
`_offsets_dir`, `_projects_path`, `_config_path`, `pending_approvals._approvals_dir`),
and every file read/write goes through that helper. Tests overriding
`BETTER_CLAUDE_HOME` mid-process therefore see every path re-resolved
on the next call. Module-load caching (the bug
`trace_collector.TRACES_DIR = ba_home() / "traces"` that A12 fixed)
is the anti-pattern this rule guards against — it broke env-var
overrides for the trace surface until A12 turned it into a lazy
`_traces_dir()` helper.

USAGE — declarative, not injected.
These Protocols are pure-Python `typing.Protocol` declarations. They
serve three purposes:

  1. Documentation of the public surface of each store, in one place.
  2. Type-check anchor for future test fakes (`class FakeSessionsStorage:
     ...` followed by `isinstance(fake, SessionsStorage)` works because
     Protocols are structural; the fake doesn't need to inherit).
  3. Forward-compat hook for the deferred A12+ work that wires
     constructor injection through a `Storage` aggregate.

The production path still calls the module-level functions directly
(`from session_store import get_session`). The Protocols do NOT replace
that today; they document it.

NOTE on scope. Per the converged plan this is "not full hex" — we
declare the ports without unwinding every call site to use a
constructor-injected port. The full hex-arch refactor would touch
hundreds of call sites and isn't justified by the present-day pain.
What IS justified: a single place where the surface of each store is
discoverable, and a structural anchor for substitution at test time.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterator, Optional, Protocol


# ============================================================================
# Sessions — backend/session_store.py
# ============================================================================
class SessionsStorage(Protocol):
    """Tree-shape session JSON persistence under `ba_home()/sessions/`.

    One file per ROOT session; forks are embedded in `forks: [Session, ...]`
    arrays (recursive). The actual single-writer abstraction is
    `session_manager.SessionManager` which wraps this; direct calls
    from production code are a code smell (see CLAUDE.md state
    ownership rule)."""

    def create_session(
        self,
        name: str = "",
        model: Optional[str] = None,
        cwd: str = "",
        orchestration_mode: str = "team",
        source: str = "web",
        provider_id: Optional[str] = None,
        browser_harness_enabled: bool = True,
        browser_harness_headless: bool = True,
        node_id: str = "primary",
    ) -> dict: ...

    def get_session(self, session_id: str) -> Optional[dict]: ...
    def get_root_tree(self, session_id: str) -> Optional[dict]: ...

    def write_session_full(
        self, root: dict, *, bump_updated_at: bool = True,
    ) -> None: ...

    def list_sessions(self) -> list[dict]: ...
    def iter_all_sessions(self) -> Iterator[dict]: ...

    def fork_session(
        self, parent_id: str, name: Optional[str] = None,
    ) -> dict: ...

    def delete_session(self, session_id: str) -> bool: ...
    def assign_message_seq(self, session: dict, message: dict) -> dict: ...


# ============================================================================
# Workers — backend/stores/worker_store.py
# ============================================================================
class WorkersStorage(Protocol):
    """Global worker registry + per-(caller, worker) fork-BC-session
    mapping at `ba_home()/workers/global.json`. Schema v6 — no
    migration; wipe to start fresh.

    Signatures here mirror `stores/worker_store.py` exactly (positional
    args, full `_agent_session_id` names, real return types). A
    Protocol that lied would let a test fake crash production callers."""

    def list_workers(self, cwd: str) -> list[dict]: ...
    def get_worker(self, cwd: str, agent_session_id: str) -> Optional[dict]: ...

    def list_worker_projection(
        self, cwd: str, limit: int = 20,
    ) -> list[dict]: ...

    def upsert_worker(
        self,
        cwd: str,
        agent_session_id: str,
        orchestration_mode: str,
        agent_sid: str,
        node_id: str = "primary",
    ) -> dict: ...

    def touch_worker(
        self,
        cwd: str,
        agent_session_id: str,
        token_usage: Optional[dict] = None,
    ) -> Optional[dict]: ...

    def remove_worker(self, cwd: str, agent_session_id: str) -> bool: ...
    def remove_worker_everywhere(self, agent_session_id: str) -> int: ...

    def get_fork_record(
        self,
        cwd: str,
        caller_agent_session_id: str,
        worker_agent_session_id: str,
    ) -> Optional[dict]: ...

    def get_fork(
        self,
        cwd: str,
        caller_agent_session_id: str,
        worker_agent_session_id: str,
    ) -> Optional[str]: ...

    def set_fork(
        self,
        cwd: str,
        caller_agent_session_id: str,
        worker_agent_session_id: str,
        fork_agent_session_id: str,
    ) -> None: ...

    def touch_fork(
        self,
        cwd: str,
        caller_agent_session_id: str,
        worker_agent_session_id: str,
    ) -> None: ...

    def clear_fork(
        self,
        cwd: str,
        caller_agent_session_id: str,
        worker_agent_session_id: str,
    ) -> bool: ...

    def clear_forks_for_worker_everywhere(
        self, worker_agent_session_id: str,
    ) -> list[str]: ...

    def clear_forks_for_caller_everywhere(
        self, caller_agent_session_id: str,
    ) -> list[str]: ...


# ============================================================================
# Approvals — backend/stores/pending_approvals.py
# ============================================================================
class ApprovalsStorage(Protocol):
    """Disk-backed fresh-worker approval queue at
    `ba_home()/pending_approvals/<delegation_id>.json`. fcntl-locked
    status transitions (`pending` → `approved` | `denied`) — multi-tab
    approve clicks are idempotent. 24h expiry."""

    def create(
        self,
        *,
        delegation_id: str,
        app_session_id: str,
        cwd: str,
        justification: str,
        proposed_description: str,
        proposed_orchestration_mode: str,
        instructions_preview: str,
        model: str,
        node_id: str = "primary",
    ) -> dict: ...

    def get(self, delegation_id: str) -> Optional[dict]: ...
    def list_pending(self, *, cwd: Optional[str] = None) -> list[dict]: ...

    def approve(
        self,
        delegation_id: str,
        *,
        description: Optional[str] = None,
        orchestration_mode: Optional[str] = None,
    ) -> tuple[Optional[dict], str]: ...

    def deny(self, delegation_id: str) -> tuple[Optional[dict], str]: ...
    def delete(self, delegation_id: str) -> bool: ...
    def prune_old(self, max_age_days: int = 7) -> int: ...


# ============================================================================
# Nodes — backend/node_store.py
# ============================================================================
class NodesStorage(Protocol):
    """In-memory live worker-node connection registry + per-(node, root)
    `last_acked_offset` persisted at `ba_home()/node_store/<node_id>.json`
    (atomic tmp+rename via a 1-second background coalescer). Holds zero
    authoritative state — the topology is the source of truth for
    which nodes EXIST; this just tracks the transient live-WS handle
    and offset cursor."""

    # Connection lifecycle
    def get_connection(self, node_id: str) -> Optional[Any]: ...
    def state(self, node_id: str) -> str: ...
    def snapshot(self) -> list[dict]: ...
    def touch_last_seen(self, node_id: str) -> None: ...

    # Listener fan-out (state transitions)
    def add_listener(
        self, cb: Callable[[str, str], Awaitable[None]],
    ) -> None: ...

    # Offset persistence (A6)
    def mark_offsets_dirty(self, node_id: str) -> None: ...
    async def flush_offsets(
        self, node_id: str, *, conn: Optional[Any] = None,
    ) -> None: ...

    # Background coalescer lifecycle
    def start_offset_flush_loop(self) -> None: ...
    async def stop_offset_flush_loop(self) -> None: ...


# ============================================================================
# Projects — backend/project_store.py
# ============================================================================
class ProjectsStorage(Protocol):
    """Sidebar Projects picker at `ba_home()/projects.json`. Auto-seeds
    from existing sessions' cwds on first read; auto-added on first
    turn of any session whose cwd isn't yet in the list."""

    def list_projects(self) -> list[dict]: ...

    def add_project(
        self, path: str, name: Optional[str] = None,
    ) -> Optional[dict]: ...

    def touch_project(self, path: str) -> None: ...
    def remove_project(self, path: str) -> bool: ...


# ============================================================================
# Config — backend/config_store.py
# ============================================================================
class ConfigStorage(Protocol):
    """Provider CRUD at `ba_home()/config.json` + macOS Keychain for
    API keys (service="better-claude", username=f"provider:{id}").
    Capability flags (`supports_fork`, `supports_manager_mode`,
    `supports_rewind`) resolved per-record-kind via
    `_kind_capabilities(kind)` and surfaced on the public providers
    list (A5)."""

    def list_providers(self) -> dict: ...
    def get_provider(self, provider_id: str) -> Optional[dict]: ...
    def get_provider_with_key(self, provider_id: str) -> Optional[dict]: ...
    def get_default_provider(self) -> Optional[dict]: ...

    def add_provider(self, payload: dict) -> dict: ...

    def update_provider(
        self, provider_id: str, payload: dict,
    ) -> Optional[dict]: ...

    def delete_provider(self, provider_id: str) -> tuple[bool, str]: ...
    def set_default_provider(self, provider_id: str) -> Optional[dict]: ...
    def add_custom_model_to_default(self, name: str) -> Optional[dict]: ...
    def apply_env_vars(self) -> None: ...


# ============================================================================
# Traces — backend/trace_collector.py
# ============================================================================
class TracesStorage(Protocol):
    """Per-turn structured traces under `ba_home()/traces/<session_id>/`
    + an `index.jsonl` for fast listing. Producer is the `TraceCollector`
    class (one instance per turn) wrapped around the manager run by
    the orchestrator. The module-level read functions below feed the
    extension-authenticated `/api/internal/traces/*` substrate and `trace_cli.py`."""

    def list_traces(
        self, session_id: Optional[str] = None, limit: int = 100,
    ) -> list[dict]: ...

    def get_trace(self, trace_id: str) -> Optional[dict]: ...
    def search_traces(self, query: str, limit: int = 50) -> list[dict]: ...

    def grep_traces(
        self,
        pattern: str,
        field: str = "all",
        session_id: Optional[str] = None,
        step_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]: ...

    def get_latest_trace(
        self, session_id: Optional[str] = None,
    ) -> Optional[dict]: ...

    def get_trace_stats(
        self, session_id: Optional[str] = None,
    ) -> dict: ...
