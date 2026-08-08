#!/usr/bin/env python3
"""Unit owner for frontend_mount.py.

frontend_mount.py is imported only at runtime by main.py / app_composition and
by four standalone (``if __name__ == "__main__"``) scripts under
backend/scripts that pytest does not collect. No pytest module owns it, so its
branch logic (SPA static-file mount, cold-build placeholder, supervisor-restart
watcher, SPA 404 fallback) has no unit-tier attribution. This file is that owner.

Strategy:
- FastAPI apps driven through fastapi.testclient.TestClient (real ASGI, no
  network, no subprocess) for the StaticFiles mount and the SPA 404 fallback.
- The async cold-build watcher is armed, popped off ``_deferred_startup_tasks``,
  and driven through ``asyncio.run`` with ``asyncio.create_task`` captured so
  the inner ``_watch_for_build`` coroutine runs to completion event-driven:
  ``dist_index.exists()`` is True, so the poll loop short-circuits with no sleep.
- Module globals (``_cold_build_watcher_armed``, ``_deferred_startup_tasks``)
  are reset per test by an autouse fixture so armed-state never leaks across
  tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import frontend_mount

_SUPERVISOR_ENV = "BETTER_AGENT_RUN_SH_SUPERVISOR"
_LEGACY_SUPERVISOR_ENV = "BETTER_CLAUDE_RUN_SH_SUPERVISOR"


@pytest.fixture(autouse=True)
def _reset_module_state():
    frontend_mount._deferred_startup_tasks.clear()
    frontend_mount._cold_build_watcher_armed = False
    yield
    frontend_mount._deferred_startup_tasks.clear()
    frontend_mount._cold_build_watcher_armed = False


# ---------- frontend_dist_dir ----------


def test_frontend_dist_dir_default_repo_layout():
    expected = Path(frontend_mount.__file__).resolve().parent.parent / "frontend" / "dist"
    assert frontend_mount.frontend_dist_dir() == expected


def test_frontend_dist_dir_frozen_pyinstaller_bundle(monkeypatch):
    monkeypatch.setattr(frontend_mount._sys, "frozen", True, raising=False)
    monkeypatch.setattr(frontend_mount._sys, "_MEIPASS", "/tmp/ba-frozen-root", raising=False)
    assert frontend_mount.frontend_dist_dir() == Path("/tmp/ba-frozen-root") / "frontend_dist"


# ---------- _NoCacheIndexStaticFiles.get_response ----------


def test_index_paths_are_non_cacheable_other_paths_keep_default(tmp_path):
    (tmp_path / "index.html").write_text("<html>shell</html>")
    (tmp_path / "foo.txt").write_text("asset-body")

    app = FastAPI()
    frontend_mount.mount_frontend(app, dist_dir=tmp_path)
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "no-store" in root.headers["cache-control"]

    index = client.get("/index.html")
    assert index.status_code == 200
    assert "no-store" in index.headers["cache-control"]

    other = client.get("/foo.txt")
    assert other.status_code == 200
    assert other.text == "asset-body"
    assert "no-store" not in other.headers.get("cache-control", "")


# ---------- _mount_cold_build_stub + _cold_build_placeholder ----------


def test_cold_build_stub_serves_placeholder_for_any_path():
    app = FastAPI()
    frontend_mount._mount_cold_build_stub(app)
    client = TestClient(app)

    r = client.get("/anything/here")
    assert r.status_code == 200
    assert "Building frontend" in r.text
    assert "no-store" in r.headers["cache-control"]


# ---------- _arm_cold_build_restart ----------


def test_arm_restart_supervisor_absent_is_noop(monkeypatch):
    monkeypatch.delenv(_SUPERVISOR_ENV, raising=False)
    monkeypatch.delenv(_LEGACY_SUPERVISOR_ENV, raising=False)

    frontend_mount._arm_cold_build_restart(FastAPI(), Path("/nonexistent/index.html"))

    assert frontend_mount._cold_build_watcher_armed is False
    assert frontend_mount._deferred_startup_tasks == []


def test_arm_restart_already_armed_is_noop(monkeypatch):
    monkeypatch.setenv(_SUPERVISOR_ENV, "1")
    frontend_mount._cold_build_watcher_armed = True

    frontend_mount._arm_cold_build_restart(FastAPI(), Path("/nonexistent/index.html"))

    assert frontend_mount._deferred_startup_tasks == []


def test_arm_restart_arms_exactly_one_watcher(monkeypatch):
    monkeypatch.setenv(_SUPERVISOR_ENV, "1")

    frontend_mount._arm_cold_build_restart(FastAPI(), Path("/nonexistent/index.html"))

    assert frontend_mount._cold_build_watcher_armed is True
    assert len(frontend_mount._deferred_startup_tasks) == 1


def _drive_watcher(start_watcher, monkeypatch) -> None:
    """Run ``_start_watcher`` under a fresh loop, capturing the inner task.

    ``asyncio.create_task`` (the module function _start_watcher calls) is
    replaced with a capturer so the spawned ``_watch_for_build`` task is
    awaited to completion event-driven. ensure_future routes through the
    running loop's own create_task, so there is no recursion.
    """

    async def _drive():
        captured: list = []

        def capture(coro):
            task = asyncio.ensure_future(coro)
            captured.append(task)
            return task

        monkeypatch.setattr(frontend_mount.asyncio, "create_task", capture)
        await start_watcher()
        if captured:
            await asyncio.gather(*captured)

    asyncio.run(_drive())


def test_watcher_completes_when_dist_already_present(monkeypatch, tmp_path):
    monkeypatch.setenv(_SUPERVISOR_ENV, "1")
    dist_index = tmp_path / "index.html"
    dist_index.write_text("shell")

    restarted: list[str] = []

    async def restart_noop(request_id):
        restarted.append(request_id)
        return None

    monkeypatch.setattr(frontend_mount, "_trigger_supervisor_restart", restart_noop)

    frontend_mount._arm_cold_build_restart(FastAPI(), dist_index)
    start_watcher = frontend_mount._deferred_startup_tasks[0]

    _drive_watcher(start_watcher, monkeypatch)

    assert restarted == [""]


class _FlippingDistIndex:
    """Path stand-in whose exists() is False for `misses` calls then True.

    Drives one poll iteration of _watch_for_build so the sleep branch runs.
    """

    def __init__(self, misses: int) -> None:
        self._remaining = misses

    def exists(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True


def test_watcher_polls_until_dist_lands(monkeypatch):
    monkeypatch.setenv(_SUPERVISOR_ENV, "1")
    # Zero interval: asyncio.sleep(0) yields once, no wall-clock delay.
    monkeypatch.setattr(frontend_mount, "_COLD_BUILD_CHECK_INTERVAL", 0)

    restarted: list[str] = []

    async def restart_noop(request_id):
        restarted.append(request_id)
        return None

    monkeypatch.setattr(frontend_mount, "_trigger_supervisor_restart", restart_noop)

    # Absent on the first poll (loop body + sleep), present on the next (exit).
    frontend_mount._arm_cold_build_restart(FastAPI(), _FlippingDistIndex(misses=1))
    start_watcher = frontend_mount._deferred_startup_tasks[0]

    _drive_watcher(start_watcher, monkeypatch)

    assert restarted == [""]


def test_watcher_swallows_restart_failure(monkeypatch, tmp_path):
    monkeypatch.setenv(_SUPERVISOR_ENV, "1")
    dist_index = tmp_path / "index.html"
    dist_index.write_text("shell")

    async def restart_boom(_request_id):
        raise RuntimeError("supervisor unavailable")

    monkeypatch.setattr(frontend_mount, "_trigger_supervisor_restart", restart_boom)

    frontend_mount._arm_cold_build_restart(FastAPI(), dist_index)
    start_watcher = frontend_mount._deferred_startup_tasks[0]

    # Must not raise: the watcher's `except Exception` logs and returns.
    _drive_watcher(start_watcher, monkeypatch)


# ---------- mount_frontend ----------


def test_mount_explicit_missing_dist_raises():
    with pytest.raises(RuntimeError):
        frontend_mount.mount_frontend(FastAPI(), dist_dir=Path("/does/not/exist/dist"))


def test_mount_present_dist_serves_spa_fallback(tmp_path):
    (tmp_path / "index.html").write_text("<html>spa-shell</html>")

    app = FastAPI()

    @app.get("/api/none")
    def _none():  # noqa: ANN202
        raise HTTPException(status_code=404, detail=None)

    frontend_mount.mount_frontend(app, dist_dir=tmp_path)
    client = TestClient(app)

    api_missing = client.get("/api/missing")
    assert api_missing.status_code == 404
    assert api_missing.json() == {"detail": "Not Found"}

    api_none_detail = client.get("/api/none")
    assert api_none_detail.status_code == 404
    assert api_none_detail.json() == {"detail": "Not Found"}

    ws_missing = client.get("/ws/missing")
    assert ws_missing.status_code == 404
    assert ws_missing.json() == {"detail": "Not Found"}

    asset = client.get("/assets/missing.js")
    assert asset.status_code == 404
    assert asset.json() == {"detail": "Not Found"}

    client_route = client.get("/some/deep/client/route")
    assert client_route.status_code == 200
    assert "spa-shell" in client_route.text


def test_mount_cold_clone_serves_stub_when_default_dist_missing(monkeypatch):
    monkeypatch.setattr(frontend_mount, "frontend_dist_dir", lambda: Path("/definitely/not/a/dist"))
    monkeypatch.delenv(_SUPERVISOR_ENV, raising=False)
    monkeypatch.delenv(_LEGACY_SUPERVISOR_ENV, raising=False)

    app = FastAPI()
    frontend_mount.mount_frontend(app)
    client = TestClient(app)

    r = client.get("/")
    assert r.status_code == 200
    assert "Building frontend" in r.text
    assert "no-store" in r.headers["cache-control"]
