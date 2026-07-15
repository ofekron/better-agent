"""Locks: a `bare_config` session (provisioned machine-completion worker /
TestApe-isolated run) must NOT auto-register its cwd as a user project,
while a normal session MUST.

Regression for the bug where the requirements extension's
provisioned worker (cwd = the extension's own install dir) leaked a
hash-named project into the user's project list.

Project ownership lives in the BFF process (see
`bff_app_routes.create_session` + `bff_runtime_contract.project_candidate_from_session`);
the runtime's `session_manager.create` no longer registers projects
directly. This test drives the BFF's `/api/sessions` route with a
mocked runtime upstream, mirroring `test_bff_projects.py`.

Run with:
    cd backend && .venv/bin/python scripts/test_bare_config_no_project.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-bareproj-")

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_app_routes  # noqa: E402
import project_store  # noqa: E402
from bff_runtime_contract import project_candidate_from_session  # noqa: E402
from bff_runtime_service import runtime_service  # noqa: E402
from bff_runtime_upstream import RuntimeUpstream  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


def _project_paths() -> set[str]:
    return {p["path"] for p in project_store.list_projects()}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="bc-test-bareproj-work-"))

    # Predicate-level checks (single source of truth: bff_runtime_contract).
    check("predicate: bare_config cwd is NOT eligible",
          project_candidate_from_session(
              {"cwd": str(work), "bare_config": True}) is None)
    check("predicate: normal cwd IS eligible",
          project_candidate_from_session(
              {"cwd": str(work), "bare_config": False}) is not None)
    check("predicate: no cwd is NOT eligible",
          project_candidate_from_session(
              {"cwd": "", "bare_config": False}) is None)

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-better-agent-bff-token"] == "service-test"
        if request.url.path == "/api/bff-runtime/projects/facts":
            return httpx.Response(200, json={"candidates": [], "aggregates": []})
        if request.url.path == "/api/bff-runtime/projects/catalog":
            body = json.loads(request.content)
            return httpx.Response(200, json=body)
        if request.url.path == "/api/bff-runtime/sessions":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": f"session-{body['cwd']}",
                "cwd": body["cwd"],
                "node_id": "primary",
                "source": "web",
                "cwd_explicit": True,
                "bare_config": body.get("bare_config", False),
            })
        raise AssertionError(request.url.path)

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), base_url="http://runtime"
    )
    runtime_service.bind(RuntimeUpstream(
        descriptor_reader=lambda: {"kind": "tcp", "host": "127.0.0.1", "port": 1},
        token_reader=lambda: "service-test",
        client_factory=lambda _descriptor: upstream_client,
    ))
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.auth_user = {"username": "ofek"}
        return await call_next(request)

    app.include_router(bff_app_routes.router)
    try:
        asyncio.run(bff_app_routes.initialize_app_projects())
        with TestClient(app) as client:
            bare_dir = work / "ext-install-dir"
            bare_dir.mkdir(parents=True, exist_ok=True)
            created = client.post(
                "/api/sessions",
                json={
                    "name": "worker:requirements:pipeline-operator",
                    "cwd": str(bare_dir),
                    "bare_config": True,
                },
            )
            check("bare_config session request succeeded",
                  created.status_code == 200)
            check("bare_config session did NOT register a project",
                  str(bare_dir.resolve()) not in _project_paths())

            normal_dir = work / "real-project"
            normal_dir.mkdir(parents=True, exist_ok=True)
            created = client.post(
                "/api/sessions",
                json={
                    "name": "Session normal",
                    "cwd": str(normal_dir),
                    "bare_config": False,
                },
            )
            check("normal session request succeeded",
                  created.status_code == 200)
            check("normal session DID register a project",
                  str(normal_dir.resolve()) in _project_paths())
    finally:
        runtime_service.unbind()
        asyncio.run(upstream_client.aclose())
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
