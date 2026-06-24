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


def test_proxy_preserves_method_path_query_and_json(monkeypatch) -> None:
    module = _load_routes_module()
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["token"] = request.headers.get("X-internal-token")
        return FakeResponse()

    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "http://core")
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", "tok")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    app = FastAPI()
    app.include_router(module.create_router(None), prefix="/provider-config-sync")
    client = TestClient(app)

    response = client.patch("/provider-config-sync/settings?cwd=/tmp/project", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["url"] == "http://core/api/internal/provider-config-sync/settings?cwd=/tmp/project"
    assert captured["method"] == "PATCH"
    assert captured["body"] == b'{"enabled":true}'
    assert captured["token"] == "tok"
