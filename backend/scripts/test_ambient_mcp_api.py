from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


home = tempfile.mkdtemp(prefix="ba-ambient-api-")
os.environ["BETTER_AGENT_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import ambient_mcp_api
import ambient_mcp_broker
import ambient_user_mcp_store
import extension_store


def payload() -> dict:
    return {
        "id": "notes",
        "name": "Notes",
        "launcher": {"command": "notes-mcp", "args": ["--stdio"], "env": {}},
        "policy": {"native_exposure": True},
        "enabled": True,
    }


def main() -> None:
    app = FastAPI()
    app.include_router(ambient_mcp_api.router)
    client = TestClient(app)
    calls: list[str] = []
    ambient_mcp_api.set_reconciler(lambda: calls.append("reconciled"))
    try:
        response = client.put("/api/ambient-mcps/user/notes", json=payload())
        assert response.status_code == 200, response.text
        assert calls == ["reconciled"]
        assert ambient_user_mcp_store.list_records()[0]["id"] == "notes"
        assert client.get("/api/ambient-mcps").status_code == 200
        listing = client.get("/api/ambient-mcps").json()
        assert listing["policy"] == {
            "share_all_eligible": True,
            "excluded_ids": [],
            "generation": 0,
            "updated_at": None,
        }

        policy = client.patch(
            "/api/ambient-mcps/policy",
            json={"share_all_eligible": True, "excluded_ids": ["user:notes"]},
        )
        assert policy.status_code == 200, policy.text
        assert policy.json()["policy"]["excluded_ids"] == ["user:notes"]
        assert policy.json()["policy"]["generation"] == 1
        assert calls[-1] == "reconciled"
        assert client.get("/api/ambient-mcps").json()["capabilities"][-1]["exposed"] is False

        stale = client.patch(
            "/api/ambient-mcps/policy",
            json={"share_all_eligible": True, "excluded_ids": ["extension:missing"]},
        )
        assert stale.status_code == 200, stale.text
        assert stale.json()["policy"]["excluded_ids"] == ["extension:missing"]
        editable = client.patch(
            "/api/ambient-mcps/policy",
            json={
                "share_all_eligible": True,
                "excluded_ids": ["extension:missing", "extension:future:server"],
            },
        )
        assert editable.status_code == 200, editable.text
        assert editable.json()["policy"]["excluded_ids"] == [
            "extension:future:server", "extension:missing"
        ]

        malformed_id = client.patch(
            "/api/ambient-mcps/policy",
            json={"share_all_eligible": True, "excluded_ids": ["bad id"]},
        )
        assert malformed_id.status_code == 422

        before_revoke_failure = client.get("/api/ambient-mcps").json()["policy"]
        real_revoke = ambient_mcp_broker.broker.revoke_extension
        reconcile_count = len(calls)
        ambient_mcp_broker.broker.revoke_extension = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("revoke down"))
        )
        try:
            revoke_failed = client.patch(
                "/api/ambient-mcps/policy",
                json={"share_all_eligible": True, "excluded_ids": ["core:ui"]},
            )
        finally:
            ambient_mcp_broker.broker.revoke_extension = real_revoke
        assert revoke_failed.status_code == 503, revoke_failed.text
        assert client.get("/api/ambient-mcps").json()["policy"] == before_revoke_failure
        assert len(calls) == reconcile_count

        revoked: list[tuple[str, str]] = []
        ambient_mcp_broker.broker.revoke_extension = lambda extension_id, *, server_name: (
            revoked.append((extension_id, server_name))
        )
        ambient_mcp_api.set_reconciler(lambda: (_ for _ in ()).throw(RuntimeError("sync down")))
        try:
            failed_policy = client.patch(
                "/api/ambient-mcps/policy",
                json={"share_all_eligible": True, "excluded_ids": ["core:ui"]},
            )
        finally:
            ambient_mcp_broker.broker.revoke_extension = real_revoke
        assert failed_policy.status_code == 503, failed_policy.text
        assert revoked == [("better-agent-core", "ui")]
        rolled_back = client.get("/api/ambient-mcps").json()["policy"]
        assert rolled_back["share_all_eligible"] is True
        assert rolled_back["excluded_ids"] == ["extension:future:server", "extension:missing"]
        assert rolled_back["generation"] == 3

        failed = client.put(
            "/api/ambient-mcps/user/notes",
            json={**payload(), "name": "Changed"},
        )
        assert failed.status_code == 503, failed.text
        assert ambient_user_mcp_store.list_records()[0]["name"] == "Notes"

        malformed = client.put(
            "/api/ambient-mcps/user/notes",
            json={**payload(), "unexpected": True},
        )
        assert malformed.status_code == 422

        ambient_mcp_api.set_reconciler(lambda: None)
        deleted = client.delete("/api/ambient-mcps/user/notes")
        assert deleted.status_code == 200
        assert ambient_user_mcp_store.list_records() == []
        assert client.delete("/api/ambient-mcps/user/notes").status_code == 404

        ambient_mcp_api._reconcile = None
        real_reconcile = extension_store.reconcile_native_mcp_servers
        extension_store.reconcile_native_mcp_servers = lambda: calls.append("lazy-default")  # type: ignore[assignment]
        try:
            ambient_mcp_api._required_reconciler()()
        finally:
            extension_store.reconcile_native_mcp_servers = real_reconcile  # type: ignore[assignment]
        assert calls[-1] == "lazy-default"
        print("PASS ambient MCP API transactions")
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
