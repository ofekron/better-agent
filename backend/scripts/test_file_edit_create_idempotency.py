"""POST /api/sessions with file_edit_enabled must honor client_session_id.

Regression lock for the duplicate-session bug: the frontend retries a
timed-out create with the same client_session_id (its idempotency key), but
the file-edit branch minted a fresh session id on every call, so each retry
created another session (observed live: three duplicates in 48s while each
POST blocked 20-28s warming the file-edit base).

Run with:
    cd backend && .venv/bin/python scripts/test_file_edit_create_idempotency.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
TMP_HOME = Path(_test_home.isolate("bc-test-file-edit-idem-"))

dist_dir = BACKEND.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import extension_store  # noqa: E402
import file_editor  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402
import working_mode  # noqa: E402
from _test_extension import install_backend_feature  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

installation_profile.allows = lambda _capability: True

_FAKE_BASES: dict[tuple[str, str, str, str], str] = {}


async def _fake_ensure_file_edit_base(cfg) -> str:
    """Deterministic stand-in for the provider-warming step (no subprocess).
    Mirrors the fixture in scripts/test_file_edit_extension_gate.py."""
    key = (cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    sid = _FAKE_BASES.get(key)
    if sid and session_manager.get(sid):
        return sid
    base = session_manager.create(
        name="file-editing-base",
        model=cfg.model,
        cwd=cfg.cwd,
        orchestration_mode="native",
        source="internal",
        provider_id=cfg.provider_id,
        reasoning_effort=cfg.reasoning_effort or None,
        node_id=cfg.node_id,
        bare_config=False,
        worker_creation_policy="deny",
    )
    fake_agent_sid = f"fake-idem-base-sid-{len(_FAKE_BASES)}"
    session_manager._run(
        base["id"],
        lambda s: s.__setitem__("agent_session_id", fake_agent_sid),
        {"kind": "test_agent_sid_set"},
    )
    working_mode.mark_working_mode(
        base["id"],
        mode=file_editor.BASE_MODE,
        meta={
            "cwd": cfg.cwd,
            "provider_id": cfg.provider_id,
            "model": cfg.model,
            "machine_completion": False,
            "version": file_editor.FILE_EDIT_BASE_SPEC.version,
            "node_id": cfg.node_id,
            "provisioned_at": time.time(),
        },
    )
    _FAKE_BASES[key] = base["id"]
    return base["id"]


file_editor._ensure_file_edit_base = _fake_ensure_file_edit_base  # type: ignore[assignment]


def check(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)
    print(f"  ok: {label}")


def _project(label: str) -> Path:
    d = TMP_HOME / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_retried_create_resolves_to_same_session(client: TestClient) -> None:
    """The user-visible bug: two POSTs carrying the same client_session_id
    (frontend timeout retry) must yield ONE session, not two."""
    project_dir = _project("retry")
    client_session_id = str(uuid.uuid4())
    payload = {
        "cwd": str(project_dir),
        "file_edit_enabled": True,
        "client_session_id": client_session_id,
    }

    first = client.post("/api/sessions", json=payload)
    check(first.status_code == 200, f"first create succeeds (got {first.status_code}: {first.text})")
    check(
        first.json()["id"] == client_session_id,
        "file-edit session is created under the client_session_id idempotency key",
    )

    second = client.post("/api/sessions", json=payload)
    check(second.status_code == 200, f"retried create succeeds (got {second.status_code}: {second.text})")
    check(
        second.json()["id"] == client_session_id,
        "retried create resolves to the same session id",
    )

    file_edit_sessions = [
        s for s in session_manager.iter_root_sessions()
        if s.get("working_mode") == file_editor.MODE
        and (s.get("working_mode_meta") or {}).get("project_cwd") == str(project_dir.resolve())
    ]
    check(
        len(file_edit_sessions) == 1,
        f"exactly one file-edit session exists for the project (got {len(file_edit_sessions)})",
    )


def test_race_loser_resumes_without_bootstrap() -> None:
    """A concurrent create that loses the id race inside file_editor must
    return the winner's session with resumed=True and no bootstrap ask or
    meta prompt (the winner owns injection)."""
    project_dir = _project("race-empty")
    winner = asyncio.run(
        file_editor.start_empty(cwd=str(project_dir), persistent=True, session_id=str(uuid.uuid4()))
    )
    sid = winner["session_id"]
    check(winner["resumed"] is False, "winner create reports resumed=False")
    check(bool(winner["user_ask"]), "winner create carries the user-facing ask")

    loser = asyncio.run(
        file_editor.start_empty(cwd=str(project_dir), persistent=True, session_id=sid)
    )
    check(loser["session_id"] == sid, "loser resolves to the winner's session")
    check(loser["resumed"] is True, "loser reports resumed=True")
    check(loser["user_ask"] is None, "loser must not repeat the user-facing ask")

    target = project_dir / "target.txt"
    target.write_text("hello\n", encoding="utf-8")
    file_winner = asyncio.run(
        file_editor.start(str(target), cwd=str(project_dir), persistent=True, session_id=str(uuid.uuid4()))
    )
    check(bool(file_winner["meta_prompt"]), "file-path winner carries the bootstrap meta prompt")
    file_loser = asyncio.run(
        file_editor.start(
            str(target), cwd=str(project_dir), persistent=True, session_id=file_winner["session_id"],
        )
    )
    check(file_loser["session_id"] == file_winner["session_id"], "file-path loser resolves to the winner's session")
    check(file_loser["resumed"] is True, "file-path loser reports resumed=True")
    check(file_loser["meta_prompt"] is None, "file-path loser must not repeat the bootstrap meta prompt")


def main_run() -> int:
    try:
        install_backend_feature(TMP_HOME, extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID)
        with TestClient(main.app) as client:
            client.headers.update({
                "Authorization": f"Bearer {auth.create_token('test')}",
            })
            test_retried_create_resolves_to_same_session(client)
            # Runs inside the app lifespan: session persistence is torn down
            # once the TestClient context exits.
            test_race_loser_resumes_without_bootstrap()
    finally:
        if created_dist:
            shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS file edit create idempotency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
