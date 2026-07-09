from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_routes_module():
    public_root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(public_root / "sdk"))
    routes_path = Path(__file__).resolve().parents[1] / "backend" / "routes.py"
    spec = importlib.util.spec_from_file_location("provider_config_sync_extension_routes", routes_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Provider Config Sync routes")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_maps_method_path_to_capability_action() -> None:
    module = _load_routes_module()
    captured = {}

    class FakeClient:
        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            captured.update(capability=capability, action=action, payload=payload, timeout=timeout)
            return {"ok": True}

    module.Client = FakeClient

    app = FastAPI()
    app.include_router(module.create_router(None), prefix="/provider-config-sync")
    client = TestClient(app)

    response = client.patch("/provider-config-sync/settings?cwd=/tmp/project", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["capability"] == "provider-config-sync"
    assert captured["action"] == "settings.patch"
    assert captured["payload"] == {"enabled": True}
