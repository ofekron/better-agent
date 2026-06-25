from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_routes_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    routes_path = Path(__file__).resolve().parents[1] / "backend" / "routes.py"
    spec = importlib.util.spec_from_file_location("ask_extension_routes", routes_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Ask routes")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ask_backend_routes_proxy_to_internal_substrate() -> None:
    module = _load_routes_module()
    calls: list[tuple[str, str, dict, float]] = []

    class FakeClient:
        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append((capability, action, dict(payload or {}), timeout))
            if action == "sessions.search":
                return {"results": [{"id": "s1"}], "reasoning": "match"}
            return {"id": "virtual:ofek-dev.ask:ask"}

    module.Client = FakeClient
    app = FastAPI()
    app.include_router(module.create_router(None))
    client = TestClient(app)

    response = client.post("/sessions/search", json={"query": "find billing"})
    assert response.status_code == 200
    assert response.json() == {"results": [{"id": "s1"}], "reasoning": "match"}
    assert calls[-1] == (
        "ask",
        "sessions.search",
        {"query": "find billing"},
        24 * 60 * 60,
    )

    response = client.post("/ask/ensure", json={})
    assert response.status_code == 200
    assert response.json() == {"id": "virtual:ofek-dev.ask:ask"}
    assert calls[-1] == (
        "ask",
        "ensure",
        {},
        10.0,
    )
