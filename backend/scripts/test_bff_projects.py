from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home


TMP_HOME = _test_home.isolate("ba-bff-projects-")

import bff_app_routes  # noqa: E402
import project_store  # noqa: E402
from bff_runtime_service import runtime_service  # noqa: E402
from bff_runtime_upstream import RuntimeUpstream  # noqa: E402


def test_bff_owns_projects_and_syncs_runtime_projection() -> None:
    work = Path(tempfile.mkdtemp(prefix="ba-bff-project-work-"))
    catalogs: list[list[dict]] = []
    requested_paths: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-better-agent-bff-token"] == "service-test"
        requested_paths.append(request.url.path)
        if request.url.path == "/api/bff-runtime/projects/facts":
            return httpx.Response(200, json={"candidates": [], "aggregates": []})
        if request.url.path == "/api/bff-runtime/projects/status":
            return httpx.Response(200, json={"aggregates": [{
                "path": str((work / "one").resolve()),
                "node_id": "primary",
                "running_count": 1,
                "unread_session_count": 2,
            }]})
        if request.url.path == "/api/bff-runtime/projects/catalog":
            body = json.loads(request.content)
            catalogs.append(body["projects"])
            return httpx.Response(200, json=body)
        if request.url.path == "/api/bff-runtime/sessions":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "session-1",
                "cwd": body["cwd"],
                "node_id": "primary",
                "source": "web",
                "cwd_explicit": True,
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
            first = client.post("/api/projects", json={"path": str(work / "one")})
            assert first.status_code == 200, first.text
            assert catalogs[-1][0]["path"] == str((work / "one").resolve())

            created = client.post(
                "/api/sessions",
                json={"name": "session", "cwd": str(work / "two")},
            )
            assert created.status_code == 200, created.text
            paths = {project["path"] for project in project_store.list_projects()}
            assert str((work / "one").resolve()) in paths
            assert str((work / "two").resolve()) in paths
            assert {project["path"] for project in catalogs[-1]} == paths

            facts_before = requested_paths.count("/api/bff-runtime/projects/facts")
            status = client.get("/api/projects/status")
            assert status.status_code == 200, status.text
            rows = {row["path"]: row for row in status.json()["projects"]}
            one = rows[str((work / "one").resolve())]
            assert one["running_count"] == 1
            assert one["unread_session_count"] == 2
            assert requested_paths[-1] == "/api/bff-runtime/projects/status"
            assert requested_paths.count("/api/bff-runtime/projects/facts") == facts_before
    finally:
        runtime_service.unbind()
        asyncio.run(upstream_client.aclose())
        shutil.rmtree(work, ignore_errors=True)


def test_bff_projects_grouped_by_worktree() -> None:
    work = Path(tempfile.mkdtemp(prefix="ba-bff-project-worktrees-"))
    repo = (work / "repo").resolve()
    wt = (work / "repo-dev").resolve()
    git_id = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), *git_id, "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt), "-b", "dev"],
        check=True, capture_output=True,
    )

    import git_repo_info

    ident = git_repo_info.repo_common_dir(str(repo))
    assert ident

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/bff-runtime/projects/facts":
            # Aggregates are keyed by repo identity (git common dir) and
            # already sum counts across every worktree of the repo.
            return httpx.Response(200, json={"candidates": [], "aggregates": [{
                "path": ident,
                "node_id": "primary",
                "running_count": 3,
                "unread_session_count": 2,
            }]})
        if request.url.path == "/api/bff-runtime/projects/catalog":
            return httpx.Response(200, json=json.loads(request.content))
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
    app.include_router(bff_app_routes.router)
    try:
        with TestClient(app) as client:
            for path in (repo, wt):
                created = client.post("/api/projects", json={"path": str(path)})
                assert created.status_code == 200, created.text

            listed = client.get("/api/projects")
            assert listed.status_code == 200, listed.text
            groups = [
                p for p in listed.json()["projects"]
                if p["path"] in {str(repo), str(wt)}
            ]
            # Both worktrees collapse into ONE project record whose path is
            # the canonical (main) worktree.
            assert len(groups) == 1, groups
            record = groups[0]
            assert record["path"] == str(repo)
            assert {w["path"] for w in record["worktrees"]} == {str(repo), str(wt)}
            assert record["running_count"] == 3
            assert record["unread_session_count"] == 2
    finally:
        runtime_service.unbind()
        asyncio.run(upstream_client.aclose())
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_bff_owns_projects_and_syncs_runtime_projection()
        test_bff_projects_grouped_by_worktree()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS test_bff_projects")
