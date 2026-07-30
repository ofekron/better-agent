#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runtime-profiles-api-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME), provider="claude")

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import config_store  # noqa: E402
import runtime_profiles_api  # noqa: E402
from auth_test_helpers import authenticate_client  # noqa: E402

BROADCASTS: list[tuple[str, dict]] = []


async def _capture_broadcast(event: str, payload: dict) -> None:
    BROADCASTS.append((event, payload))


def _profile_events() -> list[dict]:
    return [p for e, p in BROADCASTS if e == "runtime_profiles_changed"]


def main_test() -> int:
    runtime_profiles_api.configure(_capture_broadcast)
    client = TestClient(main.app)
    try:
        assert client.get("/api/runtime-profiles").status_code in (401, 403), (
            "runtime-profiles API must require auth"
        )
        authenticate_client(client)

        snapshot = client.get("/api/runtime-profiles").json()
        assert {"runtime_profiles", "default_runtime_profile_id", "deleted_providers"} <= set(snapshot)
        assert snapshot["default_runtime_profile_id"], "seeded default profile expected"
        providers = client.get("/api/providers").json()["providers"]
        provider = providers[0]
        assert len(provider["runner_options"]) > 1

        created = client.post(
            "/api/runtime-profiles",
            json={
                "provider_id": provider["id"],
                "runner": provider["runner_options"][-1],
                "default_model": "test-model",
            },
        )
        assert created.status_code == 200, created.text
        profile = created.json()
        assert profile["default_model"] == "test-model"
        serialized = json.dumps(profile)
        assert "api_key" not in serialized and "token" not in serialized
        assert _profile_events(), "creation must broadcast runtime_profiles_changed"
        assert any(
            p["id"] == profile["id"]
            for p in _profile_events()[-1]["runtime_profiles"]
        )

        assert client.post(
            "/api/runtime-profiles",
            json={"provider_id": provider["id"], "runner": provider["runner_options"][-1]},
        ).status_code == 400, "duplicate live pair must be rejected"
        assert client.post(
            "/api/runtime-profiles",
            json={"provider_id": "ghost", "runner": "native"},
        ).status_code == 400
        assert client.post(
            "/api/runtime-profiles",
            json={"provider_id": provider["id"], "runner": "bogus-runner"},
        ).status_code == 400

        patched = client.patch(
            f"/api/runtime-profiles/{profile['id']}",
            json={"name": "Renamed"},
        )
        assert patched.status_code == 200 and patched.json()["name"] == "Renamed"
        assert client.patch(
            "/api/runtime-profiles/missing", json={"name": "x"}
        ).status_code == 404

        activated = client.post(f"/api/runtime-profiles/{profile['id']}/activate")
        assert activated.status_code == 200, activated.text
        assert (
            client.get("/api/runtime-profiles").json()["default_runtime_profile_id"]
            == profile["id"]
        )
        assert client.post("/api/runtime-profiles/missing/activate").status_code == 404

        assert client.delete(
            f"/api/runtime-profiles/{profile['id']}"
        ).status_code == 409, "deleting the default profile must be refused"

        default_snapshot = client.get("/api/runtime-profiles").json()
        other = next(
            p
            for p in default_snapshot["runtime_profiles"]
            if p["id"] != profile["id"] and not p["deleted_at"]
        )
        deleted = client.delete(f"/api/runtime-profiles/{other['id']}")
        assert deleted.status_code == 200
        assert client.delete(f"/api/runtime-profiles/{other['id']}").status_code == 409
        assert client.delete("/api/runtime-profiles/missing").status_code == 404
        tombstoned = next(
            p
            for p in client.get("/api/runtime-profiles").json()["runtime_profiles"]
            if p["id"] == other["id"]
        )
        assert tombstoned["deleted_at"]
        assert client.post(
            f"/api/runtime-profiles/{other['id']}/activate"
        ).status_code == 400, "activating a tombstone must fail closed"

        full_dump = json.dumps(client.get("/api/runtime-profiles").json())
        assert "api_key" not in full_dump

        set_default_routes = [
            r.path
            for r in main.app.routes
            if getattr(r, "path", "").endswith("/set-default")
        ]
        assert not set_default_routes, "provider-level set-default must be gone"
        print("PASS runtime profiles api")
        return 0
    finally:
        client.close()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_test())
