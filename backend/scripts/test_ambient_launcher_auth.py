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


def test_launcher_auth_makes_a_backend_auth_server_ambient_eligible() -> bool:
    _record(ambient_auth="launcher")
    eligible = extension_store._native_harness_eligible(
        extension_store.get_extension(EXT_ID), "mcp", SERVER_ID
    )
    granted = True
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    except extension_store.ExtensionError:
        granted = False
    ok = eligible and granted
    print(f"{OK if ok else FAIL} ambient_auth='launcher' makes a backend-auth, user-facing "
          f"server ambient-eligible and grantable (eligible={eligible}, granted={granted})")
    _cleanup()
    return ok


def test_without_the_opt_in_the_ambient_gate_stays_closed() -> bool:
    _record(ambient_auth="")
    eligible = extension_store._native_harness_eligible(
        extension_store.get_extension(EXT_ID), "mcp", SERVER_ID
    )
    rejected = False
    try:
        extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    except extension_store.ExtensionError:
        rejected = True
    ok = (not eligible) and rejected
    print(f"{OK if ok else FAIL} without ambient_auth the ambient gate stays closed "
          f"(eligible={eligible}, grant_rejected={rejected})")
    _cleanup()
    return ok


def test_ambient_launch_resolves_and_mints_an_extension_scoped_token() -> bool:
    _record(ambient_auth="launcher")
    # Ambient exposure still requires an explicit user grant; eligibility only
    # makes the server grantable.
    extension_store.grant_native_mcp_server(EXT_ID, SERVER_ID, "global")
    record = extension_store.get_extension(EXT_ID)
    item = _item()
    available = extension_store._mcp_item_available_for_inputs(record, item, AMBIENT_INPUTS)
    config = extension_store.resolve_native_mcp_server_config(
        extension_id=EXT_ID, server_name=SERVER_ID, inputs=AMBIENT_INPUTS,
    )
    env = dict((config or {}).get("env") or {})
    token = env.get("BETTER_CLAUDE_INTERNAL_TOKEN") or ""
    import extension_token_registry
    scoped = bool(token) and extension_token_registry.resolve(token) == EXT_ID
    ok = available and bool(config) and scoped
    print(f"{OK if ok else FAIL} ambient launch resolves and mints an extension-scoped token "
          f"(available={available}, config={bool(config)}, extension_scoped={scoped})")
    _cleanup()
    return ok


def test_without_the_opt_in_ambient_resolution_is_unavailable() -> bool:
    _record(ambient_auth="")
    record = extension_store.get_extension(EXT_ID)
    available = extension_store._mcp_item_available_for_inputs(record, _item(), AMBIENT_INPUTS)
    ok = not available
    print(f"{OK if ok else FAIL} without ambient_auth an ambient launch is unavailable "
          f"(available={available})")
    _cleanup()
    return ok


def test_shipped_coordination_manifest_declares_the_opt_in() -> bool:
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
    ok = opted_in and grantable
    print(f"{OK if ok else FAIL} shipped coordination manifest is ambient-eligible AND grantable "
          f"(opted_in={opted_in}, grantable={grantable})")
    return ok


if __name__ == "__main__":
    results = [
        fn()
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} ambient launcher-auth tests passed")
    raise SystemExit(0 if all(results) else 1)
