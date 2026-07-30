#!/usr/bin/env python3
"""Every session-creating MCP tool must accept a harness profile and apply it.

`create_session` used to be the only tool that carried `harness_profile_id`
to the backend. The rest — `create_sub_session` (its registered surface
silently dropped the param), `delegate_task`, `create_worker`,
`ensure_named_worker` — minted sessions that always ran the default harness,
so a caller could not scope what a spawned session is allowed to see.

A session names a profile, never a revision of it, so a later edit to the
profile reaches every session already on it.

Two things are locked here:
  1. Surface: every session-creating tool, on every provider surface
     (communicate stdio MCP, Claude SDK tools, Codex dynamic tools, the
     better-agent runtime), takes the profile argument and forwards it.
     The registered handler is checked, not an inner helper, because the
     original bug was a wrapper that accepted fewer parameters than the
     function behind it.
  2. Effect: a session created with a profile resolves to that profile's
     server set — the selected server is present (inclusion) and a
     deselected one is gone (exclusion).
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
# communicate_mcp is a standalone stdio server; the runner puts the bundled sdk
# on PYTHONPATH the same way when it launches it.
sys.path.insert(0, str(BACKEND.parent / "sdk"))

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-session-mcp-harness-profile-")

import communicate_mcp  # noqa: E402
import extension_store  # noqa: E402
import harness_field_writer  # noqa: E402
import harness_fields  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402
import runner  # noqa: E402
import runner_better_agent  # noqa: E402
import runner_codex  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

installation_profile.integrations_enabled = lambda: True

PROFILE_PARAMS = ("harness_profile_id",)

# Tools that can mint a Better Agent session. `ask` is deliberately absent: it
# forks an existing session, which must inherit that session's own profile.
SESSION_CREATING_TOOLS = (
    "create_session",
    "create_sub_session",
    "delegate_task",
    "create_worker",
    "ensure_named_worker",
)

KEPT_EXT = "fixture.profile-kept"
KEPT_SERVER = "fixture-kept-server"
DROPPED_EXT = "fixture.profile-dropped"
DROPPED_SERVER = "fixture-dropped-server"


# ---------------------------------------------------------------- surface ---
def test_registered_communicate_tools_accept_the_profile() -> None:
    specs = {spec.name: spec for spec in communicate_mcp._specs()}
    for name in SESSION_CREATING_TOOLS:
        assert name in specs, f"{name} is not registered on the communicate MCP"
        # `_safe_result` wraps the handler; unwrap to the registered callable.
        handler = specs[name].handler
        params = inspect.signature(inspect.unwrap(handler)).parameters
        for field in PROFILE_PARAMS:
            assert field in params, (
                f"registered MCP tool {name} does not accept {field} — a wrapper "
                f"that drops it makes the profile unreachable from the model"
            )


def _captured_payload(call, **kwargs) -> dict:
    seen: dict = {}

    def fake_post_json(path, payload, timeout=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"success": True, "workers": [{"agent_session_id": "w1"}]}

    original_post = communicate_mcp._post_json
    original_env = communicate_mcp._env_required
    original_cwd = communicate_mcp._resolve_cwd
    communicate_mcp._post_json = fake_post_json
    communicate_mcp._post_mcp_job = lambda path, kind, payload, timeout=None: fake_post_json(
        path, payload, timeout,
    )
    communicate_mcp._env_required = lambda name: "sender-session"
    communicate_mcp._resolve_cwd = lambda cwd: cwd or "/tmp/project"
    try:
        call(**kwargs)
    finally:
        communicate_mcp._post_json = original_post
        communicate_mcp._env_required = original_env
        communicate_mcp._resolve_cwd = original_cwd
    return seen


def test_delegate_task_forwards_the_profile() -> None:
    seen = _captured_payload(
        communicate_mcp.delegate_task_response,
        task="do work",
        harness_profile_id="  scoped.profile  ",
    )
    assert seen["path"] == "/api/internal/delegate-task"
    assert seen["payload"]["harness_profile_id"] == "scoped.profile"


def test_create_worker_forwards_the_profile() -> None:
    seen = _captured_payload(
        communicate_mcp.create_worker_response,
        worker_description="reviewer",
        justification="needed",
        orchestration_mode="native",
        harness_profile_id="scoped.profile",
    )
    assert seen["path"] == "/api/internal/create-worker"
    assert seen["payload"]["harness_profile_id"] == "scoped.profile"


def test_ensure_named_worker_forwards_the_profile_on_the_spec() -> None:
    seen = _captured_payload(
        communicate_mcp.ensure_named_worker_response,
        name="reviewer",
        orchestration_mode="native",
        harness_profile_id="scoped.profile",
    )
    assert seen["path"] == "/api/internal/workers/provision"
    spec = seen["payload"]["workers"][0]
    assert spec["harness_profile_id"] == "scoped.profile"


def test_every_provider_surface_exposes_the_profile() -> None:
    """The three non-stdio runners declare their own tool schemas. A profile
    the model cannot name on one provider is a parity break."""
    surfaces = {
        "claude": runner,
        "codex": runner_codex,
        "better_agent": runner_better_agent,
    }
    schema_names = {
        "create_session": "_CREATE_SESSION_INPUT_SCHEMA",
        "create_sub_session": "_CREATE_SUB_SESSION_INPUT_SCHEMA",
        "create_worker": "_CREATE_WORKER_INPUT_SCHEMA",
        "delegate_task": "_DELEGATE_TASK_INPUT_SCHEMA",
        "ensure_named_worker": "_ENSURE_NAMED_WORKER_INPUT_SCHEMA",
    }
    for surface, module in surfaces.items():
        for tool, attr in schema_names.items():
            schema = getattr(module, attr)
            for field in PROFILE_PARAMS:
                assert field in schema["properties"], (
                    f"{surface} {tool} schema is missing {field}"
                )


def test_session_bridge_delegate_to_session_accepts_the_profile() -> None:
    """`delegate_to_session` with an empty session_id mints a session too."""
    server_path = BACKEND.parent / "extensions" / "session-bridge" / "mcp" / "server.py"
    source = server_path.read_text(encoding="utf-8")
    start = source.index("def delegate_to_session_response(")
    body = source[start:source.index("\ndef ", start + 1)]
    for field in PROFILE_PARAMS:
        assert f"{field}: str = \"\"" in body, f"delegate_to_session does not accept {field}"
        assert f'"{field}": {field},' in body, f"delegate_to_session does not forward {field}"


def test_routes_validate_the_selection_before_creating() -> None:
    """Every session-creating route resolves the selection through the one
    validating helper, so an unknown profile fails closed at the boundary
    instead of producing a session that cannot start a turn."""
    sources = {
        name: (BACKEND / name).read_text(encoding="utf-8")
        for name in ("workers_api.py", "internal_orchestration_api.py", "session_bridge_api.py")
    }
    for owner, marker, end_marker in (
        ("internal_orchestration_api.py", '@router.post("/api/internal/create-session")', '@router.post("/api/internal/create-sub-session")'),
        ("internal_orchestration_api.py", '@router.post("/api/internal/create-sub-session")', ''),
        ("internal_orchestration_api.py", '@router.post("/api/internal/create-worker")', '@router.post('),
        ("internal_orchestration_api.py", 'async def _handle_internal_delegate_task(', '@router.post('),
        ("workers_api.py", 'async def provision_workers_from_body(', 'def _register_provisioned_team_member('),
        ("workers_api.py", 'def _create_pending_worker_from_body(', 'async def _create_worker_from_body('),
        ("workers_api.py", 'async def _create_worker_from_body(', '@router.post('),
        ("session_bridge_api.py", 'async def _handle_internal_session_bridge_delegate(', '\n_AGENT_BOARD_MAX_PROMPT_LEN'),
    ):
        source = sources[owner]
        start = source.index(marker)
        body_start = start + len(marker)
        end = source.index(end_marker, body_start) if end_marker else len(source)
        handler = source[start:end]
        assert "_harness_profile_selection(" in handler, (
            f"{marker} does not validate its harness profile selection"
        )


# ----------------------------------------------------------------- effect ---
def _install_fixture(extension_id: str, mcp_name: str) -> None:
    package = Path(_TMP_HOME) / extension_id
    package.mkdir(parents=True, exist_ok=True)
    manifest = extension_store.validate_manifest({
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [{"name": mcp_name, "command": "mcp-fixture", "args": [], "env": {}}],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [],
        },
        "permissions": {},
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
    })
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/mcp.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-fixture",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    extension_store.grant_consent(extension_id)
    extension_store.set_mcp_server_enabled(extension_id, mcp_name, True)


def _scoped_profile() -> dict:
    """A profile that keeps one fixture server and drops the other."""
    profile = harness_profile_store.create_profile({"name": "Session MCP Scoped"})
    harness_field_writer.apply_field_writes(profile["id"], profile["revision"], [{
        "path": ["extension_instances", DROPPED_EXT, harness_fields.GROUP_MCP, DROPPED_SERVER],
        "value": False,
    }])
    return harness_profile_store.get_profile(profile["id"])


def _servers_offered_to(session_id: str) -> set[str]:
    session = session_manager.get(session_id)
    return set(extension_store.runtime_mcp_server_configs(
        {
            "app_session_id": session_id,
            "cwd": _TMP_HOME,
            "backend_url": "http://localhost:8000",
            "internal_token": "test-token",
            "resolved_harness_run_config": harness_profile_resolver.resolve_for_session(session),
        },
        user_facing=True,
        bare=False,
    ))


def test_created_session_applies_inclusion_and_exclusion() -> None:
    profile = _scoped_profile()
    scoped = session_manager.create(
        name="scoped",
        cwd=_TMP_HOME,
        orchestration_mode="native",
        model="model-x",
        harness_profile_id=profile["id"],
    )
    record = session_manager.get(scoped["id"])
    assert record["harness_profile_id"] == profile["id"]

    servers = _servers_offered_to(scoped["id"])
    assert KEPT_SERVER in servers, f"profile-selected server missing: {sorted(servers)}"
    assert DROPPED_SERVER not in servers, (
        f"profile-deselected server still offered: {sorted(servers)}"
    )


def test_created_sub_session_applies_inclusion_and_exclusion() -> None:
    """The sub-session path is what `create_sub_session` and an auto-created
    `delegate_task` target both go through."""
    profile = _scoped_profile()
    parent = session_manager.create(
        name="parent", cwd=_TMP_HOME, orchestration_mode="native", model="model-x",
    )
    sub = session_manager.create_sub_session(
        parent_session_id=parent["id"],
        name="scoped-sub",
        model="model-x",
        cwd=_TMP_HOME,
        harness_profile_id=profile["id"],
    )
    record = session_manager.get(sub["id"])
    assert record["harness_profile_id"] == profile["id"]
    assert (
        sub.get("harness_profile_snapshot")
        == record.get("harness_profile_snapshot")
    )
    snapshot = record.get("harness_profile_snapshot")
    assert isinstance(snapshot, dict), (
        "fresh sub-session did not persist its harness profile snapshot"
    )
    assert snapshot["profile_id"] == profile["id"]
    assert snapshot["launcher_projection"]["profile_id"] == profile["id"]
    assert KEPT_SERVER in snapshot["extension_mcp_servers"][KEPT_EXT]
    assert DROPPED_SERVER not in snapshot["extension_mcp_servers"].get(
        DROPPED_EXT, []
    )
    assert record.get("turn_harness_profile_snapshot") is None

    servers = _servers_offered_to(sub["id"])
    assert KEPT_SERVER in servers, f"profile-selected server missing: {sorted(servers)}"
    assert DROPPED_SERVER not in servers, (
        f"profile-deselected server still offered: {sorted(servers)}"
    )


def test_session_created_without_a_profile_keeps_every_server() -> None:
    plain = session_manager.create(
        name="plain", cwd=_TMP_HOME, orchestration_mode="native", model="model-x",
    )
    servers = _servers_offered_to(plain["id"])
    assert KEPT_SERVER in servers and DROPPED_SERVER in servers, (
        f"a session with no profile must not be narrowed: {sorted(servers)}"
    )

    parent = session_manager.create(
        name="plain-parent", cwd=_TMP_HOME, orchestration_mode="native", model="model-x",
    )
    plain_sub = session_manager.create_sub_session(
        parent_session_id=parent["id"],
        name="plain-sub",
        model="model-x",
        cwd=_TMP_HOME,
    )
    assert plain_sub["harness_profile_id"] == ""
    assert plain_sub.get("harness_profile_snapshot") is None
    assert plain_sub.get("turn_harness_profile_snapshot") is None


def main() -> int:
    try:
        _install_fixture(KEPT_EXT, KEPT_SERVER)
        _install_fixture(DROPPED_EXT, DROPPED_SERVER)
        test_registered_communicate_tools_accept_the_profile()
        test_delegate_task_forwards_the_profile()
        test_create_worker_forwards_the_profile()
        test_ensure_named_worker_forwards_the_profile_on_the_spec()
        test_every_provider_surface_exposes_the_profile()
        test_session_bridge_delegate_to_session_accepts_the_profile()
        test_routes_validate_the_selection_before_creating()
        test_created_session_applies_inclusion_and_exclusion()
        test_created_sub_session_applies_inclusion_and_exclusion()
        test_session_created_without_a_profile_keeps_every_server()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS session-creating MCP tools accept and apply harness profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
