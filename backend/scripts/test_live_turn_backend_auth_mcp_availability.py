"""Locks the fix for a live-turn availability bug: an installed+enabled
extension whose MCP entrypoint declares `requires_backend_auth: true` and
`permissions.internal_loopback: true` (e.g. the shipped `ofek-dev.memory`
extension) was structurally unreachable from any ordinary Claude/Codex/AGY
live turn.

Root cause: `_mcp_item_available_for_inputs`'s `requires_backend_auth` gate
checked `inputs.get("internal_token")`, which is unconditionally `""` at
structural-plan time for every live turn (the real per-session token is only
minted later, runner-side -- see `provider_claude.py`'s
`_build_input_payload`, `"internal_token": ""`). Only the four hardcoded
`_BROKERED_MCP_EXTENSION_IDS` bypassed this gate. Meanwhile
`_runtime_mcp_server_config_for_item` -- the very next thing evaluated after
this gate passes for the SAME item -- already mints a real per-extension
token via `needs_identity_token(record)` independent of that blank
`inputs["internal_token"]`, so the gate was rejecting items its own caller
was about to authenticate anyway.

Fix: `_extension_identity_token_mintable(record, item, inputs)` factors out
the exact minting condition and is now also accepted by the availability
gate, so a `requires_backend_auth` item that WILL get a real per-extension
token minted for it is no longer gated out for lacking a token it never
needed to read from `inputs` in the first place.

Run with:
    cd backend && .venv/bin/python scripts/test_live_turn_backend_auth_mcp_availability.py
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
_TMP_HOME = _test_home.isolate("bc-test-live-turn-backend-auth-mcp-")
_TMP_OS_HOME = tempfile.mkdtemp(prefix="bc-test-live-turn-backend-auth-mcp-os-home-")
os.environ["HOME"] = _TMP_OS_HOME

import extension_store  # noqa: E402
import installation_profile  # noqa: E402

installation_profile.integrations_enabled = lambda: True

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

EXT_ID = "ofek.backend-auth-fixture"
SERVER_ID = "backend-auth-fixture-mcp"

# The exact shape of a real live turn's structural-plan inputs (see
# provider_claude.py's `_build_input_payload`): a real, non-empty
# app_session_id, but `internal_token` unconditionally blank -- the real
# per-session token doesn't exist yet at this point.
LIVE_TURN_INPUTS = {
    "app_session_id": "real-session-id",
    "backend_url": "http://localhost:8000",
    "internal_token": "",
    "cwd": "/tmp",
    "user_facing": True,
    "bare_config": False,
}


def _record() -> None:
    entrypoint = {
        "name": SERVER_ID,
        "command": "backend-auth-fixture",
        "user_facing": True,
        "bare_allowed": True,
        "requires_backend_auth": True,
        "ambient_native": False,
    }
    manifest = extension_store.validate_manifest({
        "kind": "better-agent-extension",
        "id": EXT_ID,
        "name": "Backend Auth Fixture",
        "version": "1.0.0",
        "description": "Fixture mirroring ofek-dev.memory's requires_backend_auth MCP entrypoint.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {"mcp": [entrypoint]},
        "permissions": {"internal_loopback": True},
        "marketplace": {},
    })
    package = Path(tempfile.mkdtemp(prefix="bc-test-backend-auth-fixture-")) / "backend-auth-fixture"
    (package / "mcp").mkdir(parents=True)
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('backend auth fixture mcp')\n", encoding="utf-8")
    data = extension_store._load()
    data["extensions"][EXT_ID] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test-recorded-runtime",
            "repo_url": "",
            "extension_path": "backend-auth-fixture",
            "ref": "",
            "commit_sha": "backend-auth-fixture-test",
            "install_path": str(package),
        },
        "entitlement": {"status": "active"},
    }
    extension_store._save(data, resurrect_extension_ids={EXT_ID})


def _cleanup() -> None:
    data = extension_store._load()
    data["extensions"].pop(EXT_ID, None)
    extension_store._save(data)


def _item() -> dict:
    record = extension_store.get_extension(EXT_ID)
    return record["manifest"]["entrypoints"]["mcp"][0]


def test_requires_backend_auth_item_available_on_a_real_live_turn() -> bool:
    _record()
    record = extension_store.get_extension(EXT_ID)
    item = _item()
    available = extension_store._mcp_item_available_for_inputs(record, item, LIVE_TURN_INPUTS)
    ok = available
    print(f"{OK if ok else FAIL} a requires_backend_auth item with permissions.internal_loopback "
          f"is available on a real (non-ambient, non-brokered) live turn (available={available})")
    _cleanup()
    return ok


def test_requires_backend_auth_item_gets_a_real_minted_token_in_the_final_config() -> bool:
    _record()
    configs = extension_store.runtime_mcp_server_configs(
        LIVE_TURN_INPUTS, user_facing=True, bare=False,
    )
    config = configs.get(SERVER_ID)
    env = dict((config or {}).get("env") or {})
    token = env.get("BETTER_CLAUDE_INTERNAL_TOKEN") or ""
    import extension_token_registry
    scoped = bool(token) and extension_token_registry.resolve(token) == EXT_ID
    ok = bool(config) and scoped
    print(f"{OK if ok else FAIL} the item reaches the final live-turn mcp_servers config with a "
          f"real extension-scoped token (config={bool(config)}, extension_scoped={scoped})")
    _cleanup()
    return ok


def test_without_internal_loopback_permission_the_gate_stays_closed() -> bool:
    """The fix must not blanket-open every requires_backend_auth item --
    only ones that actually qualify for per-extension token minting
    (`needs_identity_token`, i.e. declared `permissions.internal_loopback`
    or `capabilities`). An extension that declares neither still can't
    satisfy the gate via this path."""
    entrypoint = {
        "name": SERVER_ID,
        "command": "backend-auth-fixture",
        "user_facing": True,
        "bare_allowed": True,
        "requires_backend_auth": True,
        "ambient_native": False,
    }
    manifest = extension_store.validate_manifest({
        "kind": "better-agent-extension",
        "id": EXT_ID,
        "name": "Backend Auth Fixture (no permission)",
        "version": "1.0.0",
        "description": "Fixture with no internal_loopback/capabilities permission.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {"mcp": [entrypoint]},
        "permissions": {},
        "marketplace": {},
    })
    package = Path(tempfile.mkdtemp(prefix="bc-test-backend-auth-fixture-noperm-")) / "fixture"
    (package / "mcp").mkdir(parents=True)
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('fixture')\n", encoding="utf-8")
    data = extension_store._load()
    data["extensions"][EXT_ID] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test-recorded-runtime",
            "repo_url": "",
            "extension_path": "fixture",
            "ref": "",
            "commit_sha": "backend-auth-fixture-noperm-test",
            "install_path": str(package),
        },
        "entitlement": {"status": "active"},
    }
    extension_store._save(data, resurrect_extension_ids={EXT_ID})
    record = extension_store.get_extension(EXT_ID)
    available = extension_store._mcp_item_available_for_inputs(record, _item(), LIVE_TURN_INPUTS)
    ok = not available
    print(f"{OK if ok else FAIL} a requires_backend_auth item with no mintable identity "
          f"permission still stays gated closed (available={available})")
    _cleanup()
    return ok


if __name__ == "__main__":
    results = [
        fn()
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} live-turn backend-auth mcp "
          f"availability tests passed")
    raise SystemExit(0 if all(results) else 1)
