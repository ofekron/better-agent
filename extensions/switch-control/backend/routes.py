"""Switch-control extension backend routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from better_agent_sdk import Client


def create_router(_context) -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    def get_state() -> dict[str, Any]:
        return Client().invoke_capability("switch-control", "state.get")

    @router.post("/switch")
    def switch(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        try:
            return Client().invoke_capability(
                "switch-control",
                "switch.request",
                {"target": str(body.get("target") or "").strip()},
                timeout=30.0,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
