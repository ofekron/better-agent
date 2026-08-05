"""Single read-only façade over backend's persistent stores, for
backend/adapters/* surfaces. Adapters never import session_store /
config_store / project_store / runs_dir directly — only
`backend.adapters.store_access.store_access`. See
backend/scripts/test_adapter_boundaries.py for the enforced import
boundary and this module's per-file store allowlist.

CONSTRAINT: this backend bare-imports its modules (`import session_store`),
while backend/adapters/* imports dotted (`backend.session_store`) per the
adapters boundary. backend/main.py's `_wire_surface_adapter` pre-aliases a
fixed infra set onto `backend.*` sys.modules keys before the first
`backend.adapters`/`backend.surface_contract` import so dotted access reuses
the bare-imported singleton — but main.py cannot be extended for this
module's stores in this change. `_resolve` below performs the identical
bare-import-then-alias trick, self-contained here, so a dotted import of a
store module always resolves to whatever module object the process already
has under its bare name (in-memory caches/locks/indexes included) instead of
executing the module a second time under the dotted key.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass

_CAPABILITY_PREFIX = "supports_"


def _resolve(name: str):
    dotted = f"backend.{name}"
    mod = sys.modules.get(dotted)
    if mod is not None:
        return mod
    mod = sys.modules.get(name) or importlib.import_module(name)
    sys.modules[dotted] = mod
    return mod


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    cwd: str
    archived: bool
    opened_at: str
    created_at: str
    updated_at: str
    provider_id: str | None
    kind: str


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    path: str
    node_id: str
    name: str
    git_remote: str | None
    created_at: str
    last_used: str


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    id: str
    name: str
    kind: str
    models: tuple[str, ...]
    capabilities: dict[str, bool]
    suspended: bool


@dataclass(frozen=True, slots=True)
class RuntimeProfileRecord:
    id: str
    provider_id: str
    runner: str
    name: str
    default_model: str
    default_reasoning_effort: str
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    session_id: str
    success: bool | None
    error: str | None
    started_at: float


def _session_record(raw: dict) -> SessionRecord:
    return SessionRecord(
        id=str(raw.get("id", "")),
        title=str(raw.get("name", "")),
        cwd=str(raw.get("cwd", "")),
        archived=bool(raw.get("archived", False)),
        opened_at=str(raw.get("last_opened_at", "")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        provider_id=raw.get("provider_id"),
        kind=str(raw.get("kind", "user")),
    )


def _project_record(raw: dict) -> ProjectRecord:
    return ProjectRecord(
        path=str(raw.get("path", "")),
        node_id=str(raw.get("node_id", "primary")),
        name=str(raw.get("name", "")),
        git_remote=raw.get("git_remote"),
        created_at=str(raw.get("created_at", "")),
        last_used=str(raw.get("last_used", "")),
    )


def _provider_record(raw: dict) -> ProviderRecord:
    capabilities = {
        k: v for k, v in raw.items()
        if k.startswith(_CAPABILITY_PREFIX) and isinstance(v, bool)
    }
    return ProviderRecord(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        kind=str(raw.get("kind", "claude")),
        models=tuple(raw.get("custom_models") or ()),
        capabilities=capabilities,
        suspended=bool(raw.get("suspended", False)),
    )


def _runtime_profile_record(raw: dict) -> RuntimeProfileRecord:
    return RuntimeProfileRecord(
        id=str(raw.get("id", "")),
        provider_id=str(raw.get("provider_id", "")),
        runner=str(raw.get("runner", "")),
        name=str(raw.get("name", "")),
        default_model=str(raw.get("default_model", "")),
        default_reasoning_effort=str(raw.get("default_reasoning_effort", "")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        deleted_at=raw.get("deleted_at"),
    )


def _run_record(session_id: str, run_dir) -> RunRecord:
    runs_dir = _resolve("runs_dir")
    payload = runs_dir.read_best_complete(run_dir)
    success = (
        bool(payload.get("success")) if isinstance(payload, dict) and "success" in payload else None
    )
    error = payload.get("error") if isinstance(payload, dict) else None
    try:
        started_at = run_dir.stat().st_mtime
    except OSError:
        started_at = 0.0
    return RunRecord(
        run_id=run_dir.name,
        session_id=session_id,
        success=success,
        error=error,
        started_at=started_at,
    )


class StoreAccess:
    """Narrow, typed, read-only methods the surface adapters need. No store
    internals (dicts, module objects, mutation methods) leak out."""

    def list_session_records(self) -> tuple[SessionRecord, ...]:
        session_store = _resolve("session_store")
        return tuple(_session_record(s) for s in session_store.list_sessions())

    def get_session_record(self, sid: str) -> SessionRecord | None:
        session_store = _resolve("session_store")
        raw = session_store.get_session(sid)
        return _session_record(raw) if raw is not None else None

    def list_provider_records(self) -> tuple[ProviderRecord, ...]:
        config_store = _resolve("config_store")
        providers = config_store.list_providers().get("providers", [])
        return tuple(_provider_record(p) for p in providers)

    def list_runtime_profiles(self) -> tuple[RuntimeProfileRecord, ...]:
        config_store = _resolve("config_store")
        return tuple(
            _runtime_profile_record(p) for p in config_store.list_runtime_profiles()
        )

    def list_run_records(self) -> tuple[RunRecord, ...]:
        runs_dir = _resolve("runs_dir")
        root = runs_dir.runs_root()
        by_session = runs_dir.run_dirs_by_app_session(root)
        return tuple(
            _run_record(session_id, run_dir) for session_id, run_dir in by_session.items()
        )

    def list_projects(self) -> tuple[ProjectRecord, ...]:
        project_store = _resolve("project_store")
        return tuple(_project_record(p) for p in project_store.list_projects())


store_access = StoreAccess()
