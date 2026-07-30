"""Regression: cold-build watcher routes through app_lifespan, not a legacy router hook.

Before the fix, `_arm_cold_build_restart` registered the watcher via a legacy
router startup hook. Starlette refuses to mix a lifespan with router event
handlers, which crashed uvicorn on a fresh clone (no dist, supervisor present).
The watcher is now appended to `_deferred_startup_tasks` and drained inside
`app_lifespan` after `on_startup`.

Run with:
    cd backend && .venv/bin/python scripts/test_cold_build_watcher_lifespan.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_home = tempfile.mkdtemp(prefix="ba-cold-build-watcher-")
paths.engage_test_home(_home)
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

import main  # noqa: E402
import frontend_mount  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _reset() -> None:
    frontend_mount._deferred_startup_tasks.clear()
    frontend_mount._cold_build_watcher_armed = False


def _supervisor(enabled: bool) -> None:
    # get_env checks BETTER_AGENT_ before BETTER_CLAUDE_, so toggle both names
    # to control the gate deterministically regardless of the ambient shell env.
    for name in ("BETTER_AGENT_RUN_SH_SUPERVISOR", "BETTER_CLAUDE_RUN_SH_SUPERVISOR"):
        if enabled:
            os.environ[name] = "1"
        else:
            os.environ.pop(name, None)


def run() -> None:
    app = FastAPI()

    # Supervisor absent: arming is a no-op. No router handlers, empty registry.
    _reset()
    _supervisor(False)
    dist_index = Path(tempfile.mkdtemp()) / "dist" / "index.html"
    frontend_mount._arm_cold_build_restart(app, dist_index)
    assert frontend_mount._deferred_startup_tasks == [], "no task armed without supervisor"
    assert app.router.on_startup == [], "lifespan owns startup; no legacy handler"
    assert app.router.on_shutdown == [], "lifespan owns shutdown; no legacy handler"
    assert frontend_mount._cold_build_watcher_armed is False

    # Supervisor present + dist missing: one deferred task, still no router handlers.
    _reset()
    _supervisor(True)
    dist_root = Path(tempfile.mkdtemp())
    dist_index = dist_root / "index.html"
    assert not dist_index.exists()
    frontend_mount._arm_cold_build_restart(app, dist_index)
    assert app.router.on_startup == [], "must not register legacy startup handler"
    assert app.router.on_shutdown == [], "must not register legacy shutdown handler"
    assert len(frontend_mount._deferred_startup_tasks) == 1, "exactly one deferred startup task"

    # No double-arm: a second call must not enqueue another watcher.
    frontend_mount._arm_cold_build_restart(app, dist_index)
    assert len(frontend_mount._deferred_startup_tasks) == 1, "double-arm must not duplicate"
    assert frontend_mount._cold_build_watcher_armed is True

    # Drain launches exactly one watcher; creating the dist makes it exit cleanly
    # and call _trigger_supervisor_restart("") once (mocked, no real SIGTERM).
    restart_mock = AsyncMock()
    frontend_mount._trigger_supervisor_restart = restart_mock

    async def _drive() -> None:
        captured: list[asyncio.Task] = []
        origin = asyncio.create_task

        def _capture(coro):
            task = origin(coro)
            captured.append(task)
            return task

        asyncio.create_task = _capture  # type: ignore[assignment]
        try:
            dist_root.mkdir(parents=True, exist_ok=True)
            dist_index.write_text("ok", encoding="utf-8")
            for deferred in list(frontend_mount._deferred_startup_tasks):
                await deferred()
            for task in captured:
                await task
        finally:
            asyncio.create_task = origin  # type: ignore[assignment]

        assert len(captured) == 1, f"exactly one watcher scheduled, got {len(captured)}"

    asyncio.run(_drive())
    assert restart_mock.await_count == 1, "watcher must request exactly one restart"
    assert restart_mock.await_args == call("")

    print("test_cold_build_watcher_lifespan: OK")


if __name__ == "__main__":
    run()
