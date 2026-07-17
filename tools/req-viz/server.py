"""Requirement-hierarchy visualization server.

Read-only viewer over the requirement analysis stores (units → threads →
features → products → sub-projects → projects) plus a query proxy that fires
get-requirements jobs through the running backend's capability endpoint."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "better-agent-private" / "extensions" / "requirements"))

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from paths import bc_home

app = FastAPI(title="Requirement Hierarchy Viz")
STATIC = Path(__file__).resolve().parent / "static"

BACKEND_URL = os.environ.get("BA_VIZ_BACKEND_URL", "http://127.0.0.1:18765")
QUERY_POLL_WAIT_SECONDS = 15.0


def _requirements_extension_token() -> str:
    tokens_file = bc_home() / "extension_tokens.json"
    if not tokens_file.exists():
        raise HTTPException(status_code=503, detail="extension_tokens.json not found")
    tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
    for extension_id, token in tokens.items():
        if extension_id.endswith(".requirements") and isinstance(token, str):
            return token
    raise HTTPException(status_code=503, detail="requirements extension token not found")


def _group_node(record: dict[str, Any], text_field: str = "") -> dict[str, Any]:
    text = str(record.get(text_field) or record.get("text") or record.get("title") or "")
    if not text:
        events = record.get("events") or []
        if events and isinstance(events[0], dict):
            text = str(events[0].get("text") or "")
    return {
        "id": record.get("id"),
        "text": text,
        "status": record.get("status"),
        "parent_ids": record.get("parent_ids") or [],
        "event_keys": record.get("event_keys") or [],
        "project_cwds": record.get("project_cwds") or [],
    }


@app.get("/api/graph")
def graph() -> dict[str, Any]:
    from requirement_analysis.features import load_features
    from requirement_analysis.prephase import load_units
    from requirement_analysis.products import load_products
    from requirement_analysis.projects import load_projects
    from requirement_analysis.reality import realized_threads
    from requirement_analysis.subprojects import load_subprojects

    units = [
        {
            "id": u.get("source_key"),
            "text": u.get("text"),
            "kind": u.get("kind"),
            "polarity": u.get("polarity"),
            "strength": u.get("strength"),
            "source": u.get("source"),
            "source_prompt_key": u.get("source_prompt_key"),
            "source_text": u.get("source_text"),
            "sid": u.get("sid"),
            "ts": u.get("ts"),
            "user_seq": u.get("user_seq"),
            "cwd": u.get("cwd"),
            "edited_files": u.get("edited_files") or [],
        }
        for u in load_units()
        if u.get("source_key")
    ]

    threads = []
    thread_unit_links = []
    for t in realized_threads():
        node = _group_node(t, "reality")
        node["reality_is_active"] = t.get("reality_is_active")
        node["reality_polarity"] = t.get("reality_polarity")
        threads.append(node)
        for event in t.get("events") or []:
            if isinstance(event, dict) and event.get("source_key"):
                thread_unit_links.append({"from": node["id"], "to": event["source_key"]})

    def _load_layer(loader: Any, key: str) -> list[dict[str, Any]]:
        data = loader()
        return [_group_node(r) for r in (data.get(key) or []) if isinstance(r, dict) and r.get("id")]

    features = _load_layer(load_features, "features")
    products = _load_layer(load_products, "products")
    subprojects = _load_layer(load_subprojects, "subprojects")
    projects = _load_layer(load_projects, "projects")

    def _member_links(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"from": node["id"], "to": member}
            for node in nodes
            for member in node["event_keys"]
        ]

    return {
        "layers": {
            "units": units,
            "threads": threads,
            "features": features,
            "products": products,
            "subprojects": subprojects,
            "projects": projects,
        },
        "links": {
            "thread_unit": thread_unit_links,
            "feature_thread": _member_links(features),
            "product_feature": _member_links(products),
            "subproject_member": _member_links(subprojects),
            "project_subproject": _member_links(projects),
        },
    }


def _invoke_capability(action: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    token = _requirements_extension_token()
    try:
        response = httpx.post(
            f"{BACKEND_URL}/api/internal/capabilities/invoke",
            json={"capability": "requirements", "action": action, "payload": payload},
            headers={"X-Internal-Token": token},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"backend unreachable: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"backend returned {response.status_code}")
    return response.json()


@app.post("/api/query")
def fire_query(body: dict[str, Any]) -> dict[str, Any]:
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    payload = {
        "query": query,
        "cwd": str(body.get("cwd") or ""),
        "cwds": [],
        "all_projects": bool(body.get("all_projects")),
        "wait": False,
    }
    return _invoke_capability("fire", payload, timeout=60.0)


@app.get("/api/query/{request_id}")
def query_results(request_id: str) -> dict[str, Any]:
    return _invoke_capability(
        "results",
        {"id": request_id, "wait": QUERY_POLL_WAIT_SECONDS},
        timeout=QUERY_POLL_WAIT_SECONDS + 30.0,
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("BA_VIZ_PORT", "8790")))
