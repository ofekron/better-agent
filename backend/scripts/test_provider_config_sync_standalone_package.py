"""Standalone provider-config-sync package smoke test.

Run:
    cd backend && .venv/bin/python scripts/test_provider_config_sync_standalone_package.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
_PACKAGE_SRC = _ROOT / "provider-config-sync" / "packages" / "provider-config-sync-backend" / "src"
if str(_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_SRC))

from provider_config_sync_backend import api  # noqa: E402
from provider_config_sync_backend.standalone import create_app  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'} {msg}")
    if not cond:
        FAILURES.append(msg)


async def _noop(*_args, **_kwargs):
    return None


def t_standalone_project_mcp_roundtrip() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="provider-config-sync-standalone-"))
    try:
        project = (wipe / "project").resolve()
        project.mkdir()
        sync_home = wipe / "sync-home"
        claude_mcp = project / ".mcp.json"
        claude_mcp.write_text(
            json.dumps({"mcpServers": {"demo": {"command": "echo", "args": ["ok"]}}}, indent=2) + "\n",
            encoding="utf-8",
        )
        api.configure(
            provider_records=lambda: [
                {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": str(wipe / "claude")},
                {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": str(wipe / "gemini")},
            ],
            project_records=lambda: [{"path": str(project), "node_id": "primary"}],
            sync_home=lambda: sync_home,
            encode_project_cwd=lambda value: "encoded-" + str(abs(hash(value))),
            broadcast_changed=_noop,
        )

        payload = api._discover(str(project))
        mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
        by_kind = {entry["provider_kinds"][0]: entry for entry in mcp["specifics"]}
        check(set(by_kind) == {"claude", "gemini"}, "standalone discovery finds configured providers")
        check(str(sync_home) in mcp["unified"]["path"], "unified tracking lives under injected sync home")

        asyncio.run(
            api.apply_native_file(
                api.ApplyNativeFileRequest(
                    cwd=str(project),
                    capability_id="mcp",
                    source_entry_id=by_kind["claude"]["entry_id"],
                    target_entry_id=mcp["unified"]["entry_id"],
                    expected_source=by_kind["claude"]["content"],
                    expected_target=None,
                )
            )
        )
        payload = api._discover(str(project))
        mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
        by_kind = {entry["provider_kinds"][0]: entry for entry in mcp["specifics"]}
        asyncio.run(
            api.apply_native_file(
                api.ApplyNativeFileRequest(
                    cwd=str(project),
                    capability_id="mcp",
                    source_entry_id=mcp["unified"]["entry_id"],
                    target_entry_id=by_kind["gemini"]["entry_id"],
                    expected_source=mcp["unified"]["content"],
                    expected_target=None,
                )
            )
        )
        gemini_settings = project / ".gemini" / "settings.json"
        data = json.loads(gemini_settings.read_text(encoding="utf-8"))
        check(data["mcpServers"]["demo"]["command"] == "echo", "standalone apply writes provider-native target")
    finally:
        shutil.rmtree(wipe)


def t_standalone_app_loads_json_config() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="provider-config-sync-standalone-app-"))
    try:
        project = (wipe / "project").resolve()
        project.mkdir()
        config_path = wipe / "provider-config-sync.json"
        config_path.write_text(
            json.dumps(
                {
                    "sync_home": str(wipe / "sync-home"),
                    "providers": [
                        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": str(wipe / "claude")}
                    ],
                    "projects": [{"path": str(project), "node_id": "primary"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        app = create_app(config_path)
        paths = {route.path for route in app.routes}
        check("/api/provider-config-sync" in paths, "standalone FastAPI app mounts provider-config-sync route")
    finally:
        shutil.rmtree(wipe)


def main() -> int:
    t_standalone_project_mcp_roundtrip()
    t_standalone_app_loads_json_config()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)}")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
