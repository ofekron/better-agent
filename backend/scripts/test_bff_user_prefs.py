from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import _test_home


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TMP_HOME = _test_home.isolate("ba-bff-user-prefs-")

import app_user_prefs  # noqa: E402
import bff_app_routes  # noqa: E402
from bff_runtime_service import runtime_service  # noqa: E402


def test_bff_composes_and_splits_preferences() -> None:
    runtime = {"send_mode": "queue", "session_auto_delete_days": None}
    patches: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-better-agent-bff-token"] == "service-test"
        if request.method == "PATCH":
            body = __import__("json").loads(request.content)
            patches.append(body)
            runtime.update(body)
        return httpx.Response(200, json=runtime)

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        base_url="http://runtime",
    )
    runtime_service.bind(upstream_client, "service-test")
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.auth_user = {"username": "ofek"}
        return await call_next(request)

    app.include_router(bff_app_routes.router)
    try:
        with TestClient(app) as client:
            initial = client.get("/api/user-prefs")
            assert initial.status_code == 200, initial.text
            assert initial.json()["user_display_name"] == "ofek"
            assert initial.json()["send_mode"] == "queue"

            updated = client.patch(
                "/api/user-prefs",
                json={"font_size": 16, "send_mode": "interrupt", "unknown": True},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["font_size"] == 16
            assert updated.json()["send_mode"] == "interrupt"
            assert patches == [{"send_mode": "interrupt"}]
            assert app_user_prefs.get_all()["font_size"] == 16
            rejected = client.patch(
                "/api/user-prefs",
                json={"font_size": 500, "send_mode": "queue"},
            )
            assert rejected.status_code == 400
            assert patches == [{"send_mode": "interrupt"}]
            assert (Path(TMP_HOME) / "app-state" / "user-prefs.json").exists()
            assert not (Path(TMP_HOME) / "user_prefs.json").exists()
    finally:
        runtime_service.unbind()
        asyncio.run(upstream_client.aclose())


if __name__ == "__main__":
    try:
        test_bff_composes_and_splits_preferences()
        print("PASS test_bff_user_prefs")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
