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
import project_store  # noqa: E402
import runs_dir  # noqa: E402
import session_store  # noqa: E402

from backend.adapters.store_access import StoreAccess, store_access  # noqa: E402

_PUBLIC_METHODS = {
    "list_session_records",
    "get_session_record",
    "list_provider_records",
    "list_runtime_profiles",
    "list_run_records",
    "list_projects",
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
    assert sys.modules["backend.session_store"] is session_store
    assert sys.modules["backend.config_store"] is config_store
    assert sys.modules["backend.project_store"] is project_store
    assert sys.modules["backend.runs_dir"] is runs_dir


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
    test_list_run_records_from_seeded_run_dir,
    test_list_projects_reflects_seeded_project,
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
