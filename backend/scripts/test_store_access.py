#!/usr/bin/env python3
"""Unit coverage for backend/adapters/store_access.py — the single
read-only store-access façade for surface adapters.

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched), no LLM/provider subprocess involved. Seeds
state through the stores' own public write APIs (bare-imported, matching
the rest of backend/scripts), then reads it back through
`backend.adapters.store_access.store_access` (dotted) to prove
`store_access._resolve`'s bare/dotted aliasing reuses those SAME bare
module singletons rather than executing a second, empty copy under the
dotted key — see store_access.py's module docstring.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_store_access.py -q
    PYTHONPATH=. python3 backend/scripts/test_store_access.py   # __main__ fallback
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-store-access-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import config_store  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
import llm_call_log  # noqa: E402
import project_store  # noqa: E402
import runs_dir  # noqa: E402
import session_store  # noqa: E402
import user_prefs  # noqa: E402
from stores import worker_store  # noqa: E402  (bare — matches main.py's `from stores import task_store`)

from backend.adapters.store_access import StoreAccess, store_access  # noqa: E402

_PUBLIC_METHODS = {
    "list_session_records",
    "get_session_record",
    "list_provider_records",
    "list_runtime_profiles",
    "get_default_runtime_profile_id",
    "list_deleted_providers",
    "get_last_models",
    "get_last_reasoning_efforts",
    "list_run_records",
    "get_latest_run_record",
    "inspect_process_tree",
    "get_session_tree_records",
    "project_session_counts",
    "list_projects",
    "get_provider_credential_status",
    "get_worker_record",
    "list_llm_call_records",
    "list_pending_interactions",
    "list_pending_interactions_for_sessions",
    # ADR 0008 folders/tags/session-organization reads.
    "list_folders",
    "list_tags",
    "get_session_organization",
    "get_session_organizations",
    # ADR 0011 (System & Host Surface) reads.
    "list_extension_records",
    "get_extension_record",
    "extension_updates_available_ids",
    "list_extension_ui_hooks",
    "list_extension_frontend_modules",
    "list_harness_profile_records",
    "marketplace_bridge_snapshot",
    "list_schedule_records",
    "list_host_startup_tasks",
    "list_installation_capability_records",
    "list_node_records",
    "node_provider_credential_records",
    "list_pending_node_registrations",
}


def test_list_session_records_reflects_seeded_session() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-store-access-cwd-")
    session = session_store.create_session(name="hello world", model="m", cwd=cwd)

    records = store_access.list_session_records()
    match = next((r for r in records if r.id == session["id"]), None)
    assert match is not None
    assert match.title == "hello world"
    assert match.cwd == cwd
    assert match.archived is False


def test_get_session_record_single_lookup() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-store-access-cwd-")
    session = session_store.create_session(name="single lookup", model="m", cwd=cwd)

    record = store_access.get_session_record(session["id"])
    assert record is not None
    assert record.id == session["id"]
    assert record.title == "single lookup"

    assert store_access.get_session_record(f"missing-{uuid.uuid4().hex}") is None


def test_list_provider_records_and_runtime_profiles_from_one_provider() -> None:
    provider = config_store.add_provider({
        "name": "Test Provider", "kind": "claude", "mode": "subscription",
        "custom_models": ["model-a", "model-b"],
    })

    providers = store_access.list_provider_records()
    match = next((p for p in providers if p.id == provider["id"]), None)
    assert match is not None
    assert match.name == "Test Provider"
    assert match.kind == "claude"
    assert set(match.models) == {"model-a", "model-b"}
    assert match.capabilities  # kind defaults populate at least one supports_* flag
    assert all(isinstance(v, bool) for v in match.capabilities.values())

    profiles = store_access.list_runtime_profiles()
    profile_match = next((p for p in profiles if p.provider_id == provider["id"]), None)
    assert profile_match is not None
    assert profile_match.runner


def test_list_runtime_profiles_include_deleted_surfaces_tombstones() -> None:
    provider = config_store.add_provider({
        "name": "Tombstone Provider", "kind": "claude", "mode": "subscription",
    })
    default_id = config_store.get_default_runtime_profile_id()
    live_before = store_access.list_runtime_profiles()
    profile = next(p for p in live_before if p.provider_id == provider["id"])
    assert profile.id != default_id, "must delete a non-default profile — deletion refuses the default"

    deleted, reason = config_store.delete_runtime_profile(profile.id)
    assert deleted, reason

    live_after = store_access.list_runtime_profiles()
    assert not any(p.id == profile.id for p in live_after), "default (False) must hide tombstones"

    with_deleted = store_access.list_runtime_profiles(include_deleted=True)
    match = next((p for p in with_deleted if p.id == profile.id), None)
    assert match is not None, "include_deleted=True must surface the tombstone"
    assert match.deleted_at is not None
    assert match.name == profile.name


def test_get_default_runtime_profile_id_reflects_config_store() -> None:
    config_store.add_provider({"name": "Default Src", "kind": "claude", "mode": "subscription"})
    expected = config_store.get_default_runtime_profile_id()
    assert expected is not None
    assert store_access.get_default_runtime_profile_id() == expected


def test_list_deleted_providers_reflects_the_graveyard() -> None:
    # `config_store.delete_provider` requires a live credential-broker
    # transaction this bare test harness doesn't run (same environment gap
    # `test_provider_suspension.py`'s native-routing failure documents) —
    # unrelated to what this test verifies (the READ side, a thin wrapper
    # over `config_store.list_deleted_providers()`). Seeds the graveyard
    # directly via the same private state load/save `_bury_providers`
    # itself writes through, matching that function's own record shape.
    state = config_store._load_state()
    state.setdefault("deleted_providers", []).append({
        "id": "doomed-provider-id", "name": "Doomed Provider",
        "nickname": "doomed-nick", "kind": "claude",
        "deleted_at": "2026-01-01T00:00:00+00:00",
    })
    config_store._save_state(state)

    graveyard = store_access.list_deleted_providers()
    match = next((r for r in graveyard if r.id == "doomed-provider-id"), None)
    assert match is not None
    assert match.name == "Doomed Provider"
    assert match.nickname == "doomed-nick"
    assert match.kind == "claude"
    assert match.deleted_at == "2026-01-01T00:00:00+00:00"


def test_get_last_models_and_reasoning_efforts_reflect_user_prefs() -> None:
    provider = config_store.add_provider({"name": "Prefill Src", "kind": "claude", "mode": "subscription"})
    profile = next(p for p in store_access.list_runtime_profiles() if p.provider_id == provider["id"])

    user_prefs.set_last_model(profile.id, "model-x")
    user_prefs.set_last_reasoning_effort(profile.id, "high")

    assert store_access.get_last_models().get(profile.id) == "model-x"
    assert store_access.get_last_reasoning_efforts().get(profile.id) == "high"


def test_list_run_records_from_seeded_run_dir() -> None:
    root = runs_dir.runs_root()
    run_id = f"run-{uuid.uuid4().hex}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    app_session_id = f"app-{uuid.uuid4().hex}"
    runs_dir.atomic_write_json(run_dir / "state.json", {
        "session_id": f"prov-{run_id}",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": app_session_id,
    })
    runs_dir.atomic_write_json(run_dir / "complete.json", {"success": True, "error": None})

    records = store_access.list_run_records()
    match = next((r for r in records if r.session_id == app_session_id), None)
    assert match is not None
    assert match.run_id == run_id
    assert match.success is True
    assert match.error is None


def test_list_projects_reflects_seeded_project() -> None:
    project_dir = tempfile.mkdtemp(prefix="ba-store-access-project-")
    added = project_store.add_project(project_dir, name="my project")
    assert added is not None

    projects = store_access.list_projects()
    match = next((p for p in projects if p.path == added["path"]), None)
    assert match is not None
    assert match.name == "my project"
    assert match.node_id == "primary"


def test_get_latest_run_record_picks_most_recent() -> None:
    root = runs_dir.runs_root()
    app_session_id = f"app-{uuid.uuid4().hex}"
    older = f"run-{uuid.uuid4().hex}"
    newer = f"run-{uuid.uuid4().hex}"
    for run_id in (older, newer):
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        runs_dir.atomic_write_json(run_dir / "state.json", {
            "session_id": f"prov-{run_id}",
            "jsonl_path": str(run_dir / "stream.jsonl"),
            "app_session_id": app_session_id,
        })
    # `started_at` is the run dir's mtime — stamp it explicitly so ordering
    # doesn't depend on same-second filesystem resolution.
    os.utime(root / older, (1000.0, 1000.0))
    os.utime(root / newer, (2000.0, 2000.0))

    match = store_access.get_latest_run_record(app_session_id)
    assert match is not None
    assert match.run_id == newer

    assert store_access.get_latest_run_record(f"missing-{uuid.uuid4().hex}") is None


def test_get_provider_credential_status_reflects_api_key_provider() -> None:
    provider = config_store.add_provider({
        "name": f"Credential Provider {uuid.uuid4().hex}", "kind": "claude", "mode": "api_key",
    })
    # No desktop credential-session connection exists in this test process
    # (backend.credential_session_client._CONNECTION is only set from the
    # BETTER_AGENT_CREDENTIAL_SESSION_FD env var) — `available()` is
    # deterministically False, so the status is deterministically "blocked".
    status = store_access.get_provider_credential_status(provider["id"])
    assert status == "blocked"


def test_get_worker_record_reflects_seeded_worker() -> None:
    cwd = tempfile.mkdtemp(prefix="ba-store-access-worker-cwd-")
    agent_session_id = f"worker-{uuid.uuid4().hex}"
    worker_store.upsert_worker(cwd, agent_session_id, "native", None, name="my worker")

    record = store_access.get_worker_record(agent_session_id)
    assert record is not None
    assert record.agent_session_id == agent_session_id
    assert record.name == "my worker"
    assert record.cwd == cwd
    assert record.orchestration_mode == "native"
    assert record.token_usage == {}

    assert store_access.get_worker_record(f"missing-{uuid.uuid4().hex}") is None


def test_list_llm_call_records_reflects_seeded_call() -> None:
    call = llm_call_log.append_call(
        source="turn", reason="test", provider_id="prov-1", model="model-x",
        trace_id="tr_abc123", token_usage={"input_tokens": 10, "output_tokens": 5},
    )

    records = store_access.list_llm_call_records()
    match = next((r for r in records if r.id == call["id"]), None)
    assert match is not None
    assert match.provider_id == "prov-1"
    assert match.model == "model-x"
    assert match.trace_id == "tr_abc123"
    assert match.token_usage == {"input_tokens": 10, "output_tokens": 5}


def test_resolve_aliases_bare_modules_onto_backend_namespace() -> None:
    """Every store_access method must resolve to the SAME bare-imported
    module object this test already seeded through, not a second copy
    minted under the dotted `backend.<name>` key (see store_access.py's
    module docstring for why a second copy would silently diverge)."""
    store_access.list_session_records()
    store_access.list_provider_records()
    store_access.list_runtime_profiles()
    store_access.list_run_records()
    store_access.list_projects()
    store_access.get_worker_record(f"missing-{uuid.uuid4().hex}")
    store_access.list_llm_call_records()
    assert sys.modules["backend.session_store"] is session_store
    assert sys.modules["backend.config_store"] is config_store
    assert sys.modules["backend.project_store"] is project_store
    assert sys.modules["backend.runs_dir"] is runs_dir
    assert sys.modules["backend.worker_store"] is worker_store
    assert sys.modules["backend.llm_call_log"] is llm_call_log


def test_store_access_exposes_only_read_methods() -> None:
    public_methods = {
        name for name in dir(StoreAccess)
        if not name.startswith("_") and callable(getattr(StoreAccess, name))
    }
    assert public_methods == _PUBLIC_METHODS


_TESTS = [
    test_list_session_records_reflects_seeded_session,
    test_get_session_record_single_lookup,
    test_list_provider_records_and_runtime_profiles_from_one_provider,
    test_list_runtime_profiles_include_deleted_surfaces_tombstones,
    test_get_default_runtime_profile_id_reflects_config_store,
    test_list_deleted_providers_reflects_the_graveyard,
    test_get_last_models_and_reasoning_efforts_reflect_user_prefs,
    test_list_run_records_from_seeded_run_dir,
    test_get_latest_run_record_picks_most_recent,
    test_get_provider_credential_status_reflects_api_key_provider,
    test_get_worker_record_reflects_seeded_worker,
    test_list_projects_reflects_seeded_project,
    test_list_llm_call_records_reflects_seeded_call,
    test_resolve_aliases_bare_modules_onto_backend_namespace,
    test_store_access_exposes_only_read_methods,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
