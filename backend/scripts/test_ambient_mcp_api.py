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

        ambient_mcp_api.set_reconciler(lambda: (_ for _ in ()).throw(RuntimeError("sync down")))
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
