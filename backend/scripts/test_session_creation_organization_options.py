from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_test_home.isolate("bc-test-session-creation-organization-")
os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "sender"
os.environ["BETTER_CLAUDE_CWD"] = "/repo"

import communicate_mcp  # noqa: E402
import orchestration_tool_schemas  # noqa: E402
import runner  # noqa: E402
import runner_better_agent  # noqa: E402
import runner_codex  # noqa: E402
import session_organization_store  # noqa: E402


ORGANIZATION_FIELDS = {"folder_id", "tag_ids"}


def _assert_schema(schema: dict) -> None:
    properties = schema["properties"]
    assert ORGANIZATION_FIELDS <= properties.keys()
    assert properties["folder_id"]["type"] == "string"
    assert properties["tag_ids"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "OPTIONAL - tag ids to assign only if a new session is created.",
    }


def test_store_validates_and_assigns_together() -> None:
    folder = session_organization_store.create_folder(
        project_id="/repo", name="Folder",
    )
    tag = session_organization_store.create_tag(project_id="/repo", name="Tag")
    session_organization_store.set_session_organization(
        "session-1", folder["id"], [tag["id"], tag["id"]],
    )
    organization = session_organization_store.organization_for_session("session-1")
    assert organization["folder_id"] == folder["id"]
    assert organization["tag_ids"] == [tag["id"]]

    try:
        session_organization_store.validate_session_organization(
            folder["id"], ["missing-tag"],
        )
    except ValueError as exc:
        assert str(exc) == "unknown tag_id"
    else:
        raise AssertionError("unknown tag must be rejected")


def test_provider_schema_parity() -> None:
    shared = orchestration_tool_schemas
    for schema in (
        shared.DELEGATE_TASK_INPUT_SCHEMA,
        shared.ENSURE_NAMED_WORKER_INPUT_SCHEMA,
        runner._CREATE_WORKER_INPUT_SCHEMA,
        runner._CREATE_SESSION_INPUT_SCHEMA,
        runner._CREATE_SUB_SESSION_INPUT_SCHEMA,
        runner_codex._CREATE_WORKER_INPUT_SCHEMA,
        runner_codex._CREATE_SESSION_INPUT_SCHEMA,
        runner_codex._CREATE_SUB_SESSION_INPUT_SCHEMA,
    ):
        _assert_schema(schema)


def test_mcp_servers_schema_and_payload_parity() -> None:
    # Regression lock: mcp_servers (the per-session extension-MCP opt-in, e.g.
    # 'testape-internal') must reach the POST payload for every provider's
    # in-process create_session/create_sub_session tool, not just Claude's.
    # A Codex session missing this silently drops the opt-in with no error.
    for schema in (
        runner._CREATE_SESSION_INPUT_SCHEMA,
        runner._CREATE_SUB_SESSION_INPUT_SCHEMA,
        runner_codex._CREATE_SESSION_INPUT_SCHEMA,
        runner_codex._CREATE_SUB_SESSION_INPUT_SCHEMA,
        runner_better_agent._CREATE_SESSION_INPUT_SCHEMA,
        runner_better_agent._CREATE_SUB_SESSION_INPUT_SCHEMA,
    ):
        assert "mcp_servers" in schema["properties"], schema


def test_fastmcp_creation_signatures() -> None:
    for response in (
        communicate_mcp.delegate_task_response,
        communicate_mcp.create_worker_response,
        communicate_mcp.ensure_named_worker_response,
        communicate_mcp.create_session_response,
        communicate_mcp.create_sub_session_response,
    ):
        assert ORGANIZATION_FIELDS <= inspect.signature(response).parameters.keys()


def test_fastmcp_creation_payloads() -> None:
    captured: list[tuple[str, dict]] = []

    def fake_post_json(endpoint: str, payload: dict, timeout: float) -> dict:
        captured.append((endpoint, payload))
        if endpoint == "/api/internal/workers/provision":
            return {"workers": [{"agent_session_id": "worker", "created": True}]}
        return {"success": True}

    def fake_post_job(
        endpoint: str, _job: str, payload: dict, timeout: float,
    ) -> dict:
        captured.append((endpoint, payload))
        return {"success": True}

    communicate_mcp._post_json = fake_post_json
    communicate_mcp._post_mcp_job = fake_post_job
    kwargs = {"folder_id": "folder", "tag_ids": ["tag"]}
    communicate_mcp.create_session_response("session", **kwargs)
    communicate_mcp.create_sub_session_response(**kwargs)
    communicate_mcp.create_worker_response("worker", "needed", "native", **kwargs)
    communicate_mcp.ensure_named_worker_response("named", "native", **kwargs)
    communicate_mcp.delegate_task_response("task", **kwargs)
    assert len(captured) == 5
    for _endpoint, payload in captured:
        organization = payload["workers"][0] if "workers" in payload else payload
        assert organization["folder_id"] == "folder"
        assert organization["tag_ids"] == ["tag"]


def main() -> int:
    test_store_validates_and_assigns_together()
    test_provider_schema_parity()
    test_mcp_servers_schema_and_payload_parity()
    test_fastmcp_creation_signatures()
    test_fastmcp_creation_payloads()
    print("PASS session creation organization options")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
