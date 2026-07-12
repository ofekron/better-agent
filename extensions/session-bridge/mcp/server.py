"""Session-bridge extension MCP surface."""
from __future__ import annotations

import time
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from better_agent_sdk import Client

# Match core's per-endpoint budgets: session_search runs up to 15 min (+30s
# headroom so this client never preempts the search budget); delegate can drive
# a whole session turn (24h); provision is short.
_SEARCH_TIMEOUT = 15 * 60 + 30
_DELEGATE_TIMEOUT = 24 * 60 * 60
_PROPOSE_TIMEOUT = 10.0


class SessionBridgeClient:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client or Client()

    @property
    def app_session_id(self) -> str:
        return self._client.app_session_id

    def target_session(self, explicit: str = "") -> str:
        target = str(explicit or "").strip()
        bound = str(self.app_session_id or "").strip()
        if bound and target and target != bound:
            raise ValueError("target session does not match the bound Better Agent session")
        target = target or bound
        if not target:
            raise ValueError("app_session_id is required for ambient use")
        return target

    def invoke(self, action: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        capability_payload = dict(payload)
        if action in {"sessions.search", "delegate"}:
            capability_payload["app_session_id"] = self.target_session(
                str(capability_payload.get("app_session_id") or "")
            )
        return self._client.invoke_capability(
            "session-bridge",
            action,
            capability_payload,
            timeout=timeout,
        )

    def invoke_durable(
        self,
        action: str,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        job_id = f"mcp_{uuid.uuid4().hex}"
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            response = self.invoke(
                action,
                {**payload, "_mcp_job_id": job_id, "_mcp_job_wait": 0},
                timeout=min(30.0, max(1.0, timeout)),
            )
        except Exception:
            response = self._client.invoke_capability(
                "core",
                "mcp-jobs.results",
                {
                    "operation": operation,
                    "id": job_id,
                    "_mcp_job_wait": 0,
                },
                timeout=min(30.0, max(1.0, timeout)),
            )
        while isinstance(response, dict) and response.get("ready") is False:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return response
            time.sleep(min(1.0, max(0.05, remaining)))
            response = self._client.invoke_capability(
                "core",
                "mcp-jobs.results",
                {
                    "operation": operation,
                    "id": job_id,
                    "_mcp_job_wait": min(5.0, max(0.0, remaining)),
                },
                timeout=min(30.0, max(1.0, remaining)),
            )
        if isinstance(response, dict) and response.get("ready") is True and "result" in response:
            result = response.get("result")
            return result if isinstance(result, dict) else {"success": False, "error": "MCP job returned invalid result"}
        return response


def search_sessions_response(
    query: str,
    limit: int = 5,
    *,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    node_id: str = "",
    app_session_id: str = "",
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"results": [], "error": "empty_query"}
    payload: dict[str, Any] = {"query": query, "limit": limit, "app_session_id": app_session_id}
    # Only forward non-empty filters so an unset param never constrains.
    for key, val in (
        ("provider_id", provider_id),
        ("model", model),
        ("reasoning_effort", reasoning_effort),
        ("node_id", node_id),
    ):
        if isinstance(val, str) and val.strip():
            payload[key] = val.strip()
    try:
        client = SessionBridgeClient()
        payload["app_session_id"] = client.target_session(app_session_id)
        result = client.invoke_durable(
            "sessions.search",
            "session-bridge-search",
            payload,
            timeout=_SEARCH_TIMEOUT,
        )
    except Exception as exc:  # tool boundary: surface transport failures, never crash
        return {"results": [], "error": str(exc)}
    return _compact_search_response(result)


def _compact_search_response(result: dict[str, Any]) -> dict[str, Any]:
    response = {"results": result.get("results") or []}
    reasoning = result.get("reasoning")
    if reasoning:
        response["reasoning"] = reasoning
    error = result.get("error")
    if error:
        response["error"] = error
    return response


def delegate_to_session_response(
    prompt: str,
    run_mode: str,
    approval: str,
    session_id: str = "",
    display_prompt: str = "",
    source: str = "",
    client_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    app_session_id: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    try:
        client = SessionBridgeClient()
        return client.invoke_durable(
            "delegate",
            "session-bridge-delegate",
            {
                "session_id": session_id,
                "prompt": prompt,
                "display_prompt": display_prompt,
                "source": source,
                "client_id": client_id,
                "run_mode": run_mode,
                "approval": approval,
                "provider_id": provider_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "app_session_id": client.target_session(app_session_id),
                "folder_id": (folder_id or "").strip() or None,
                "tag_ids": tag_ids or [],
            },
            timeout=_DELEGATE_TIMEOUT,
        )
    except Exception as exc:  # tool boundary: surface transport failures, never crash
        return {"success": False, "error": str(exc)}


def propose_sessions_response(
    session_ids: list[str],
    reasoning: str = "",
    proposed_project_path: str = "",
    app_session_id: str = "",
) -> dict[str, Any]:
    client = SessionBridgeClient()
    try:
        return client.invoke(
            "sessions.propose",
            {
                "caller_sid": client.target_session(app_session_id),
                "session_ids": session_ids or [],
                "reasoning": reasoning,
                "proposed_project_path": proposed_project_path,
            },
            timeout=_PROPOSE_TIMEOUT,
        )
    except Exception as exc:  # tool boundary: surface transport failures, never crash
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP("better-agent-session-bridge")

    @server.tool()
    def search_sessions(
        query: str,
        limit: int = 5,
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        node_id: str = "",
        app_session_id: str = "",
    ) -> dict[str, Any]:
        """Find which of the user's OTHER sessions are relevant to a query, ranked
        by relevance. Discovery only — returns session ids/metadata to act on with
        delegate_to_session or propose_sessions.

        Optional exact-match filters narrow the candidate set (empty / unset =
        no constraint): `provider_id` (e.g. "claude", "openai"), `model`
        (e.g. "claude-sonnet-4-5"), `reasoning_effort`, `node_id`. Use these
        to scope a search to sessions run on a specific provider/model."""
        return search_sessions_response(
            query,
            limit,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
            node_id=node_id,
            app_session_id=app_session_id,
        )

    @server.tool()
    def delegate_to_session(
        prompt: str,
        run_mode: str,
        approval: str,
        session_id: str = "",
        display_prompt: str = "",
        source: str = "",
        client_id: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        app_session_id: str = "",
        folder_id: str = "",
        tag_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a prompt against ANY user-chosen session (fork / continue / new) and
        WAIT for its result, returned inline. The cross-session, user-driven
        counterpart to delegate_task — unlike delegate_task (detached, team-routed),
        this blocks and returns the answer."""
        return delegate_to_session_response(
            prompt,
            run_mode,
            approval,
            session_id,
            display_prompt,
            source,
            client_id,
            provider_id,
            model,
            reasoning_effort,
            app_session_id,
            folder_id,
            tag_ids,
        )

    @server.tool()
    def propose_sessions(
        session_ids: list[str],
        reasoning: str = "",
        proposed_project_path: str = "",
        app_session_id: str = "",
    ) -> dict[str, Any]:
        """Present sessions you chose to the user as an inline picker so they decide
        which to act on. Use after search_sessions when the choice should be the
        user's, not yours."""
        return propose_sessions_response(
            session_ids, reasoning, proposed_project_path, app_session_id
        )

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
