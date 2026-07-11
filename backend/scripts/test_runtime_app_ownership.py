from __future__ import annotations

import ast
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home


_TEST_HOME = _test_home.isolate(prefix="ba-runtime-app-ownership-")


def _route_paths(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not isinstance(target, ast.Attribute):
                continue
            if not isinstance(target.value, ast.Name):
                continue
            if target.value.id not in {"app", "router"} or not decorator.args:
                continue
            value = decorator.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                paths.add(value.value)
    return paths


def test_file_drafts_are_bff_owned() -> None:
    backend = Path(__file__).resolve().parents[1]
    runtime_routes = _route_paths(backend / "main.py")
    bff_routes = _route_paths(backend / "bff_app_routes.py")
    assert "/api/file/draft" not in runtime_routes
    assert "/api/file/draft" in bff_routes


def test_ui_selection_is_bff_owned() -> None:
    backend = Path(__file__).resolve().parents[1]
    runtime_routes = _route_paths(backend / "main.py")
    bff_routes = _route_paths(backend / "bff_app_routes.py")
    assert "/api/ui-selection" not in runtime_routes
    assert "/api/ui-selection" in bff_routes
    assert "import ui_selection" not in (backend / "main.py").read_text(encoding="utf-8")


def test_bff_draft_round_trip_needs_no_runtime() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import bff_app_routes

    app = FastAPI()
    app.include_router(bff_app_routes.router)
    client = TestClient(app)
    response = client.post(
        "/api/file/draft",
        json={
            "path": "/tmp/example.txt",
            "node_id": "primary",
            "content": "draft",
            "base_identity": {"mtime_ns": 1, "size": 2},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "draft"
    loaded = client.get(
        "/api/file/draft",
        params={"path": "/tmp/example.txt", "node_id": "primary"},
    )
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["content"] == "draft"


if __name__ == "__main__":
    try:
        test_file_drafts_are_bff_owned()
        test_ui_selection_is_bff_owned()
        test_bff_draft_round_trip_needs_no_runtime()
        print("PASS: app-owned routes execute in the BFF only")
    finally:
        shutil.rmtree(_TEST_HOME)
