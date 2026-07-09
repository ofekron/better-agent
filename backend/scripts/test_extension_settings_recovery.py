#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = Path(_test_home.isolate("ba-test-extension-settings-recovery-"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import extension_api  # noqa: E402
import extension_store  # noqa: E402


async def _no_broadcast() -> None:
    return None


def main() -> None:
    package = HOME / "supervisor-package"
    (package / "ui").mkdir(parents=True)
    (package / "ui" / "index.js").write_text("export const ok = true;\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek-dev.supervisor"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek-dev.supervisor",
            "name": "Supervisor",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.js",
                "frontend_modules": [{
                    "slot": "input-overflow-menu",
                    "id": "supervisor-controls",
                    "label": "Supervisor",
                    "kind": "module",
                    "module": "ui/index.js",
                }, {
                    "slot": "chat-inline-actions",
                    "id": "supervisor-verdict",
                    "label": "Supervisor verdict",
                    "kind": "module",
                    "module": "ui/index.js",
                }],
            },
            "permissions": {},
            "marketplace": {},
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {"type": "git", "install_path": str(package), "commit_sha": "test"},
        "entitlement": {"status": "not_required"},
        "runtime": {"status": "ready"},
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    settings_path = extension_store._ext_settings_path()  # type: ignore[attr-defined]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"schema_version": 1, "extensions": {"ofek-dev.supervisor": {}}}),
        encoding="utf-8",
    )

    original_broadcast = extension_api._broadcast_extensions_changed
    extension_api._broadcast_extensions_changed = _no_broadcast
    app = FastAPI()
    app.include_router(extension_api.router)
    app.get("/healthy")(lambda: {"ok": True})
    try:
        with TestClient(app) as client:
            response = client.get("/api/extensions/frontend-entrypoints")
            assert response.status_code == 409, (response.status_code, response.text, settings_path)
            detail = response.json()["detail"]
            revision = detail.pop("revision")
            assert len(revision) == 64
            assert detail == {
                "error": "extension_settings_incompatible",
                "message": "Extension settings are incompatible with this Better Agent version",
                "found_schema": 1,
                "expected_schema": 2,
                "reset_available": True,
            }
            assert client.get("/healthy").json() == {"ok": True}

            settings_path.write_text(
                json.dumps({"schema_version": 2, "extensions": {}}),
                encoding="utf-8",
            )
            stale_reset = client.post("/api/extensions/settings/reset", json={
                "expected_found_schema": 1,
                "expected_revision": revision,
            })
            assert stale_reset.status_code == 409
            assert settings_path.exists()

            settings_path.write_text(
                json.dumps({"schema_version": 1, "extensions": {"ofek-dev.supervisor": {}}}),
                encoding="utf-8",
            )
            current = client.get("/api/extensions/frontend-entrypoints").json()["detail"]
            reset = client.post("/api/extensions/settings/reset", json={
                "expected_found_schema": current["found_schema"],
                "expected_revision": current["revision"],
            })
            assert reset.status_code == 200
            assert reset.json() == {"schema_version": 2}
            assert not settings_path.exists()

            recovered = client.get("/api/extensions/frontend-entrypoints")
            assert recovered.status_code == 200
            modules = recovered.json()["entrypoints"][0]["frontend_modules"]
            assert {(item["slot"], item["id"]) for item in modules} == {
                ("input-overflow-menu", "supervisor-controls"),
                ("chat-inline-actions", "supervisor-verdict"),
            }
            assert all("/api/extensions/ofek-dev.supervisor/frontend/ui/index.js" in item["module_url"] for item in modules)
    finally:
        extension_api._broadcast_extensions_changed = original_broadcast
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(main))
    print("extension settings recovery passed")
