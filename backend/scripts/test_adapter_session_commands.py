#!/usr/bin/env python3
"""Failing-first coverage for `backend/session_commands.py` — the concrete
`SessionCommandPort` implementation (ADR 0008 command plane, Package B6).
Proves each method is a real, single-path wrapper around the SAME
`session_detail_api.py`/`projects_api.py` mutation functions the legacy
REST routes call (not a second, parallel implementation), and that
project mutations publish the new `project.fire.*` owner-side fact
`SessionSurfaceAdapter._on_project_fire` projects into `ProjectUpsert`/
`ProjectRemoved`.

Isolated via `paths.engage_test_home`, same idiom as the sibling
`test_adapter_session_gaps.py`/`test_surface_adapters.py`.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_session_commands.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_session_commands.py
"""

from __future__ import annotations

import asyncio
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

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-session-commands-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import project_store  # noqa: E402
import projects_api  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

import session_commands  # noqa: E402
from backend.adapters.session_adapter import SessionSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.session_surface import ProjectRemoved, ProjectUpsert  # noqa: E402


async def _noop_notify() -> None:
    return None


async def _noop_broadcast(_event_type: str, _payload: dict) -> None:
    return None


async def _empty_aggregate_snapshot() -> dict:
    return {"epoch": "e", "revision": 0, "aggregates": {}}


def _configure_projects_api() -> None:
    projects_api.configure(_noop_notify, _noop_broadcast, _empty_aggregate_snapshot)


def _project_dir() -> str:
    """A fresh tempdir already in `project_store`'s canonical form
    (`node_path_authority.canonical_node_path`'s `Path(...).resolve()`) —
    on macOS `tempfile.mkdtemp()` returns a `/var/...` path that resolves
    to `/private/var/...`, so comparing the raw mkdtemp path against a
    stored/broadcast project_ref would spuriously mismatch."""
    return str(Path(tempfile.mkdtemp(prefix="ba-session-commands-project-")).resolve())


_PORT = session_commands.build_session_command_port()


# ---------------------------------------------------------------------------
# Validation-only rejections — no legacy mechanism to route to.
# ---------------------------------------------------------------------------

def test_assign_project_without_project_ref_rejects() -> None:
    result = asyncio.run(_PORT.assign_project("some-session", None))
    assert result.accepted is False
    assert result.code == "unsupported"


def test_create_project_without_path_rejects() -> None:
    result = asyncio.run(_PORT.create_project("a name", ""))
    assert result.accepted is False
    assert result.code == "invalid_path"


def test_rename_project_without_ref_rejects() -> None:
    result = asyncio.run(_PORT.rename_project("", "new name"))
    assert result.accepted is False
    assert result.code == "invalid_project_ref"


def test_delete_project_without_ref_rejects() -> None:
    result = asyncio.run(_PORT.delete_project(""))
    assert result.accepted is False
    assert result.code == "invalid_project_ref"


# ---------------------------------------------------------------------------
# Real create/rename/delete round-trips through project_store, with the
# project.fire.* fact observed live by SessionSurfaceAdapter.
# ---------------------------------------------------------------------------

def test_create_project_real_round_trip_broadcasts_project_upsert() -> None:
    _configure_projects_api()
    path = _project_dir()
    name = f"created via port {uuid.uuid4().hex}"

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        result = asyncio.run(_PORT.create_project(name, path))
        assert result.accepted, result
        stored = next(p for p in project_store.list_projects() if p["path"] == path)
        assert stored["name"] == name

        upserts = [f for f in received if isinstance(f, ProjectUpsert)]
        assert upserts, received
        assert upserts[-1].project.project_ref == path
        assert upserts[-1].project.name == name
    finally:
        sub.close()


def test_rename_project_real_round_trip_reuses_upsert_mutation() -> None:
    """Same function `create_project` uses (`project_store.add_project`
    upserts by path) — proves rename_project is not a second, parallel
    mutation path."""
    _configure_projects_api()
    path = _project_dir()
    asyncio.run(_PORT.create_project("original name", path))

    new_name = f"renamed {uuid.uuid4().hex}"
    result = asyncio.run(_PORT.rename_project(path, new_name))
    assert result.accepted, result
    stored = next(p for p in project_store.list_projects() if p["path"] == path)
    assert stored["name"] == new_name


def test_delete_project_real_round_trip_broadcasts_project_removed() -> None:
    _configure_projects_api()
    path = _project_dir()
    asyncio.run(_PORT.create_project("to be deleted", path))

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        result = asyncio.run(_PORT.delete_project(path))
        assert result.accepted, result
        assert not any(p["path"] == path for p in project_store.list_projects())

        removed = [f for f in received if isinstance(f, ProjectRemoved)]
        assert removed, received
        assert removed[-1].project_ref == path
    finally:
        sub.close()

    # Deleting an already-gone project rejects rather than reporting a
    # false success.
    second = asyncio.run(_PORT.delete_project(path))
    assert second.accepted is False
    assert second.code == "not_found"


# ---------------------------------------------------------------------------
# Real session mutations — session_detail_api's own functions, unchanged.
# ---------------------------------------------------------------------------

def test_archive_session_real_round_trip() -> None:
    session = session_store.create_session(
        name=f"archive via port {uuid.uuid4().hex}",
        cwd=tempfile.mkdtemp(prefix="ba-session-commands-cwd-"),
    )
    sid = session["id"]

    result = asyncio.run(_PORT.archive_session(sid, True))
    assert result.accepted, result
    assert session_manager.get(sid)["archived"] is True

    result2 = asyncio.run(_PORT.archive_session(sid, False))
    assert result2.accepted, result2
    assert session_manager.get(sid)["archived"] is False


def test_archive_session_missing_session_rejects() -> None:
    result = asyncio.run(_PORT.archive_session(f"missing-{uuid.uuid4().hex}", True))
    assert result.accepted is False
    assert result.code == "404"


def test_rename_session_real_round_trip() -> None:
    session = session_store.create_session(
        name=f"before rename {uuid.uuid4().hex}",
        cwd=tempfile.mkdtemp(prefix="ba-session-commands-cwd-"),
    )
    sid = session["id"]
    new_title = f"after rename {uuid.uuid4().hex}"

    result = asyncio.run(_PORT.rename_session(sid, new_title))
    assert result.accepted, result
    assert session_manager.get(sid)["name"] == new_title


def test_mark_opened_real_round_trip() -> None:
    session = session_store.create_session(
        name=f"opened via port {uuid.uuid4().hex}",
        cwd=tempfile.mkdtemp(prefix="ba-session-commands-cwd-"),
    )
    sid = session["id"]
    assert not session_manager.get(sid).get("last_opened_at")

    result = asyncio.run(_PORT.mark_opened(sid))
    assert result.accepted, result
    assert session_manager.get(sid).get("last_opened_at")


_TESTS = [
    test_assign_project_without_project_ref_rejects,
    test_create_project_without_path_rejects,
    test_rename_project_without_ref_rejects,
    test_delete_project_without_ref_rejects,
    test_create_project_real_round_trip_broadcasts_project_upsert,
    test_rename_project_real_round_trip_reuses_upsert_mutation,
    test_delete_project_real_round_trip_broadcasts_project_removed,
    test_archive_session_real_round_trip,
    test_archive_session_missing_session_rejects,
    test_rename_session_real_round_trip,
    test_mark_opened_real_round_trip,
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
