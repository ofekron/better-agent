"""A profile's per-extension MCP toggle must reach the servers a run offers.

The projection already excluded a deselected server, and the run payload
already carried that exclusion — but `_mcp_server_configs_for_delivery`
computed `profile_selected` and then only used it to WIDEN the native
delivery path. On the runtime path (the one that builds the provider CLI's
`mcpServers`) nothing narrowed, so switching a server off in a profile left
it in the model's tool surface.

Locks both directions: the deselected server is gone, every other server the
profile keeps is untouched, and a run with no resolved profile is unchanged.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-mcp-selection-")

import extension_store  # noqa: E402
import harness_field_writer  # noqa: E402
import harness_fields  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402

installation_profile.integrations_enabled = lambda: True

KEPT_EXT = "fixture.mcp-kept"
KEPT_SERVER = "fixture-kept-server"
DROPPED_EXT = "fixture.mcp-dropped"
DROPPED_SERVER = "fixture-dropped-server"


def _install_fixture(extension_id: str, mcp_name: str) -> None:
    """A minimal runtime-ready extension with one MCP entrypoint, installed
    through the store internals so the real readiness predicates apply."""
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
    # Enabled globally, so Default offers it and the profile is the only thing
    # that can take it away.
    extension_store.set_mcp_server_enabled(extension_id, mcp_name, True)


def _runtime_servers(profile_id: str | None) -> set[str]:
    """The runtime MCP server names a turn on this profile would offer."""
    session = {"harness_profile_id": profile_id} if profile_id else {}
    return set(extension_store.runtime_mcp_server_configs(
        _run_inputs(harness_profile_resolver.resolve_for_session(session)),
        user_facing=True,
        bare=False,
    ))


def _run_inputs(snapshot: dict | None = None) -> dict:
    """The inputs shape a real turn hands the launcher. The backend url and
    token are what make a `requires_backend_auth` server available at all."""
    inputs = {
        "app_session_id": "sid",
        "cwd": _TMP_HOME,
        "backend_url": "http://localhost:8000",
        "internal_token": "test-token",
    }
    if snapshot is not None:
        inputs["resolved_harness_run_config"] = snapshot
    return inputs


def test_default_offers_every_fixture_server() -> None:
    servers = _runtime_servers(harness_profile_store.DEFAULT_PROFILE_ID)
    assert KEPT_SERVER in servers, f"Default dropped {KEPT_SERVER}: {sorted(servers)}"
    assert DROPPED_SERVER in servers, f"Default dropped {DROPPED_SERVER}: {sorted(servers)}"


def test_profile_deselection_removes_the_server_from_the_run() -> None:
    profile = harness_profile_store.create_profile({"name": "MCP Scoped"})
    harness_field_writer.apply_field_writes(profile["id"], profile["revision"], [{
        "path": ["extension_instances", DROPPED_EXT, harness_fields.GROUP_MCP, DROPPED_SERVER],
        "value": False,
    }])
    servers = _runtime_servers(profile["id"])
    assert DROPPED_SERVER not in servers, (
        f"a profile-deselected MCP server is still offered to the model: {sorted(servers)}"
    )
    assert KEPT_SERVER in servers, (
        f"deselecting one server dropped an unrelated one: {sorted(servers)}"
    )


def test_run_without_a_resolved_profile_is_unfiltered() -> None:
    """Inputs that carry no resolved profile have no selection to honor —
    filtering them on an empty projection would strip every server."""
    servers = set(extension_store.runtime_mcp_server_configs(
        _run_inputs(), user_facing=True, bare=False,
    ))
    assert KEPT_SERVER in servers and DROPPED_SERVER in servers, (
        f"unresolved inputs must not be filtered: {sorted(servers)}"
    )


def test_projection_that_governs_nothing_is_unfiltered() -> None:
    """A turn resolved before any extension was runtime-ready carries a
    profile id with an empty projection. That means "nothing was scoped
    yet", not "no server was selected" — filtering on it strips every
    server from an ordinary turn."""
    servers = set(extension_store.runtime_mcp_server_configs(
        _run_inputs({"profile_id": "default", "launcher_projection": {
            "profile_id": "default",
            "extension_revisions": {},
            "extension_mcp_servers": {},
        }}),
        user_facing=True,
        bare=False,
    ))
    assert KEPT_SERVER in servers and DROPPED_SERVER in servers, (
        f"an empty projection must not strip the run's servers: {sorted(servers)}"
    )


def main() -> int:
    try:
        _install_fixture(KEPT_EXT, KEPT_SERVER)
        _install_fixture(DROPPED_EXT, DROPPED_SERVER)
        test_default_offers_every_fixture_server()
        test_profile_deselection_removes_the_server_from_the_run()
        test_run_without_a_resolved_profile_is_unfiltered()
        test_projection_that_governs_nothing_is_unfiltered()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS harness profile MCP selection reaches the run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
