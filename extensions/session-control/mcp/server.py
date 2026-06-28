"""Session-control extension MCP surface.

Agent-self tools that let the running agent steer its OWN session:
switch model/provider/reasoning_effort, or request a continuation into a
fresh provider subprocess under the same Better Agent session. Both delegate
to core's /api/internal/session-control/* endpoints via the SDK loopback;
core owns the session-state write, this is the authorized trigger.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from better_agent_sdk import Client

_TIMEOUT = 30.0


def switch_model_response(
    model: str = "",
    provider_id: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    # Only forward non-empty selectors so an unset param is a no-op for that
    # field — core fails closed on an unknown model/provider.
    for key, val in (
        ("model", model),
        ("provider_id", provider_id),
        ("reasoning_effort", reasoning_effort),
    ):
        val = str(val or "").strip()
        if val:
            payload[key] = val
    if not payload:
        return {"success": False, "error": "at least one of model, provider_id, reasoning_effort is required"}
    try:
        return Client().call_internal(
            "/api/internal/session-control/selectors",
            payload,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # tool boundary: surface transport failures, never crash
        return {"success": False, "error": str(exc)}


def continue_in_fresh_context_response(prompt: str) -> dict[str, Any]:
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}
    try:
        return Client().call_internal(
            "/api/internal/session-control/continue-fresh",
            {"prompt": prompt},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # tool boundary: surface transport failures, never crash
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP("better-agent-session-control")

    @server.tool()
    def switch_model(
        model: str = "",
        provider_id: str = "",
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        """Switch THIS session's model, provider, and/or reasoning effort. The
        change persists to the session and takes effect on the next turn, which
        runs in a fresh provider subprocess under the same session. At least one
        of model / provider_id / reasoning_effort must be set. Use this to pick a
        stronger, cheaper, or differently-capable model mid-task."""
        return switch_model_response(model, provider_id, reasoning_effort)

    @server.tool()
    def continue_in_fresh_context(prompt: str) -> dict[str, Any]:
        """Request a continuation: after the current turn finishes, start a FRESH
        provider subprocess under the SAME session (chained to the prior one) and
        run `prompt` in it. Use this when the context window is filling up and you
        want to shed history while keeping the same session. Provide the prompt the
        fresh subprocess should continue with (gather any needed prior context
        yourself via your tools first)."""
        return continue_in_fresh_context_response(prompt)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
