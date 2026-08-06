"""Locks the `ambient_auth: "launcher"` opt-in that lets a backend-auth,
user-facing MCP entrypoint (the coordination extension's `lock_ops`) resolve
in an ambient — session-less — native launch.

The invariant under test: opting in changes WHO mints the token, never
WHETHER one is required. Ambient launches still authenticate, with an
extension-scoped token minted by `extension_mcp_launcher` at connect time, so
no secret is written into the on-disk native config and the backend principal
stays the extension (owner-based lock ops remain refused). Without the opt-in
the ambient gate stays closed exactly as before.

Run with:
    cd backend && .venv/bin/python scripts/test_ambient_launcher_auth.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ambient-launcher-auth-")
_TMP_OS_HOME = tempfile.mkdtemp(prefix="bc-test-ambient-launcher-auth-os-home-")
os.environ["HOME"] = _TMP_OS_HOME

import extension_store  # noqa: E402
import native_mcp_grants  # noqa: E402
import installation_profile  # noqa: E402

installation_profile.integrations_enabled = lambda: True

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

EXT_ID = "ofek.ambient-auth-fixture"
SERVER_ID = "coordination"

# An ambient launch: the launcher resolves the config with no app_session_id.
AMBIENT_INPUTS = {
    "app_session_id": "",
    "backend_url": "http://localhost:8000",
    "internal_token": "",
    "cwd": "/tmp",
    "user_facing": False,
    "bare_config": False,
    "extension_mcp_launcher_context": True,
}


def _record(*, ambient_auth: str) -> Path:
    entrypoint = {
        "name": SERVER_ID,
        "command": "coordination-stub",
        "user_facing": True,
        "bare_allowed": True,
        "requires_backend_auth": True,
        "ambient_native": True,
    }
    if ambient_auth:
        entrypoint["ambient_auth"] = ambient_auth
    package = Path(tempfile.mkdtemp(prefix="bc-test-ambient-auth-")) / "ambient-auth-fixture"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": EXT_ID,
        "name": "Ambient Auth Fixture",
        "version": "1.0.0",
        "description": "Fixture for ambient launcher-auth tests.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {"mcp": [entrypoint]},
        "permissions": {"internal_loopback": True, "native_mcp": {SERVER_ID: ["global"]}},
        "marketplace": {},
    }
    # Without the opt-in this manifest is rejected outright, so the
    # no-opt-in fixture is stored unvalidated to exercise the resolution
    # gates rather than the validator.
    stored = (
        extension_store.validate_manifest(manifest)
        if ambient_auth
        else {**manifest, "entrypoints": {"mcp": [dict(entrypoint)]}}
    )
    (package / "better-agent-extension.json").write_text(json.dumps(stored), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('coordination mcp')\n", encoding="utf-8")
    data = extension_store._load()
    data["extensions"][EXT_ID] = {
        "manifest": stored,
        "enabled": True,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test-recorded-runtime",
            "repo_url": "",
            "extension_path": "ambient-auth-fixture",
            "ref": "",
            "commit_sha": "ambient-auth-fixture-test",
            "install_path": str(package),
        },
        "entitlement": {"status": "active"},
    }
    extension_store._save(data, resurrect_extension_ids={EXT_ID})
    return package


def _cleanup() -> None:
    native_mcp_grants.remove_grants_for_extension(EXT_ID)
    data = extension_store._load()
    data["extensions"].pop(EXT_ID, None)
    extension_store._save(data)


def _item() -> dict:
    record = extension_store.get_extension(EXT_ID)
    return record["manifest"]["entrypoints"]["mcp"][0]


def test_launcher_auth_makes_a_backend_auth_server_ambient_eligible() -> None:
    _record(ambient_auth="launcher")
    try:
        eligible = extension_store._native_harness_eligible(
            extension_store.get_extension(EXT_ID), "mcp", SERVER_ID
        )
        granted = True
        try:
            extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
        except extension_store.ExtensionError:
            granted = False
        assert eligible, f"server should be ambient-eligible with ambient_auth='launcher' (eligible={eligible})"
        assert granted, f"server should be grantable (granted={granted})"
    finally:
        _cleanup()


def test_without_the_opt_in_the_ambient_gate_stays_closed() -> None:
    _record(ambient_auth="")
    try:
        eligible = extension_store._native_harness_eligible(
            extension_store.get_extension(EXT_ID), "mcp", SERVER_ID
        )
        rejected = False
        try:
            extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
        except extension_store.ExtensionError:
            rejected = True
        assert not eligible, f"ambient gate should stay closed without ambient_auth (eligible={eligible})"
        assert rejected, f"grant should be rejected without ambient_auth (grant_rejected={rejected})"
    finally:
        _cleanup()


def test_ambient_launch_resolves_and_mints_an_extension_scoped_token() -> None:
    _record(ambient_auth="launcher")
    # Ambient exposure still requires an explicit user grant; eligibility only
    # makes the server grantable.
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
        record = extension_store.get_extension(EXT_ID)
        item = _item()
        available = extension_store._mcp_item_available_for_inputs(record, item, AMBIENT_INPUTS)
        assert available, "ambient launch should be available for an opted-in, granted server"
        config = extension_store.resolve_native_mcp_server_config(
            extension_id=EXT_ID, server_name=SERVER_ID, inputs=AMBIENT_INPUTS,
        )
        assert config, "ambient launch should resolve a native mcp config"
        env = dict(config.get("env") or {})
        token = env.get("BETTER_CLAUDE_INTERNAL_TOKEN") or ""
        import extension_token_registry
        assert token, "ambient launch should mint an extension-scoped token"
        assert extension_token_registry.resolve(token) == EXT_ID, "minted token must scope to the extension"
    finally:
        _cleanup()


def test_brokered_extension_still_mints_a_token_on_a_genuine_ambient_launch() -> None:
    """A brokered extension_id (coordination, session-bridge, session-control,
    marketplace) normally authenticates in-session via the runtime broker, not
    a self-minted token -- but the runtime broker only exists inside a
    BA-orchestrated run. On a genuinely ambient (session-less) launch there is
    no broker to fall back to, so ambient_auth='launcher' must still mint an
    extension-scoped token exactly as it does for a non-brokered extension.
    Regression test: brokered-ness used to unconditionally suppress minting
    (`manifest["id"] not in _BROKERED_MCP_EXTENSION_IDS`, with no ambient
    carve-out), so the shipped coordination extension's `ambient_auth:
    "launcher"` opt-in silently resolved with no token at all -- it deleted
    the entrypoint's ambient path in production."""
    _record(ambient_auth="launcher")
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
        original_brokered = extension_store._BROKERED_MCP_EXTENSION_IDS
        extension_store._BROKERED_MCP_EXTENSION_IDS = original_brokered | {EXT_ID}
        try:
            config = extension_store.resolve_native_mcp_server_config(
                extension_id=EXT_ID, server_name=SERVER_ID, inputs=AMBIENT_INPUTS,
            )
            assert config, "ambient launch should resolve a config even for a brokered extension"
            env = dict(config.get("env") or {})
            token = env.get("BETTER_CLAUDE_INTERNAL_TOKEN") or ""
            import extension_token_registry
            assert token, "brokered extension must still self-mint on a genuine ambient launch"
            assert extension_token_registry.resolve(token) == EXT_ID, "minted token must scope to the extension"
        finally:
            extension_store._BROKERED_MCP_EXTENSION_IDS = original_brokered
    finally:
        _cleanup()


def test_brokered_extension_in_session_still_relies_on_the_broker() -> None:
    """The fix must not widen self-minting into the normal in-session path for
    brokered extensions -- only the genuinely ambient (no app_session_id)
    launch gets the carve-out. A session-bound resolution for a brokered
    extension must still NOT self-mint."""
    _record(ambient_auth="launcher")
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
        original_brokered = extension_store._BROKERED_MCP_EXTENSION_IDS
        extension_store._BROKERED_MCP_EXTENSION_IDS = original_brokered | {EXT_ID}
        session_inputs = {**AMBIENT_INPUTS, "app_session_id": "real-session-id", "user_facing": True}
        try:
            config = extension_store.resolve_native_mcp_server_config(
                extension_id=EXT_ID, server_name=SERVER_ID, inputs=session_inputs,
            )
            assert config, "session-bound launch should resolve a config"
            env = dict(config.get("env") or {})
            assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in env, (
                "brokered extension in-session must NOT self-mint; it relies on the runtime broker"
            )
        finally:
            extension_store._BROKERED_MCP_EXTENSION_IDS = original_brokered
    finally:
        _cleanup()


def test_without_the_opt_in_ambient_resolution_is_unavailable() -> None:
    _record(ambient_auth="")
    try:
        record = extension_store.get_extension(EXT_ID)
        available = extension_store._mcp_item_available_for_inputs(record, _item(), AMBIENT_INPUTS)
        assert not available, f"ambient launch should be unavailable without ambient_auth (available={available})"
    finally:
        _cleanup()


def test_shipped_coordination_manifest_declares_the_opt_in() -> None:
    manifest_path = Path(_BACKEND).parent / "extensions" / "coordination" / "better-agent-extension.json"
    manifest = extension_store.validate_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    item = manifest["entrypoints"]["mcp"][0]
    opted_in = item["ambient_native"] is True and item["ambient_auth"] == "launcher"
    # Eligibility alone is not enough: without a permissions.native_mcp
    # declaration the server can never be granted, so ambient exposure stays
    # impossible no matter how the entrypoint is flagged.
    server_id = item.get("replaces_builtin") or item["name"]
    declarations = extension_store.native_mcp_declarations(
        {"manifest": manifest, "source": {"install_path": str(manifest_path.parent)}}
    )
    declared = declarations.get((manifest["id"], server_id))
    grantable = declared is not None and "global" in declared.scopes
    assert opted_in, "shipped coordination manifest must declare ambient_auth='launcher'"
    assert grantable, "shipped coordination manifest must declare a grantable global native_mcp scope"


if __name__ == "__main__":
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL}  {name}\n  {exc}")
        except Exception:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"{FAIL}  {name}")
        else:
            print(f"{OK}  {name}")
    print()
    if failed:
        print(f"{failed} of {len(tests)} ambient launcher-auth test(s) FAILED")
    else:
        print(f"all {len(tests)} ambient launcher-auth tests passed")
    raise SystemExit(1 if failed else 0)
