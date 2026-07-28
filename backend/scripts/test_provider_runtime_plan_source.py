from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provider_runtime_capability_model import reject_secrets  # noqa: E402
from provider_runtime_plan_source import (  # noqa: E402
    selected_runtime_agent_sources,
    selected_runtime_skill_sources,
    structural_provider_runtime_plan,
)


def _record(package: Path) -> dict:
    return {
        "enabled": True,
        "generation": "extension-generation",
        "revision": 7,
        "permission_grants": {"backend_routes": True},
        "manifest": {
            "id": "example.extension",
            "version": "1.2.3",
            "entrypoints": {
                "settings": [
                    {"key": "label", "type": "string", "default": ""},
                    {"key": "access_token", "type": "secret"},
                ],
                "mcp": [{
                    "name": "example-mcp",
                    "predicate": {"equals": {"working_mode": "build"}},
                }],
            },
        },
        "runtime": {"package_root": str(package)},
    }


def test_selected_sources_are_exact_and_respect_gates() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        agent = root / "agent.md"
        agent.write_text("agent", encoding="utf-8")

        with (
            patch("installation_profile.integrations_enabled", return_value=True),
            patch(
                "runtime_skills._discover_skills",
                return_value=[
                    {"name": "keep", "dir": str(skill)},
                    {"name": "drop", "dir": str(skill)},
                ],
            ),
            patch(
                "runtime_agents._discover_agents",
                return_value=[{"claude": str(agent)}],
            ),
        ):
            assert selected_runtime_skill_sources(
                str(root),
                bare_config=False,
                disabled=["drop"],
            ) == {"keep": skill.resolve()}
            assert selected_runtime_agent_sources(
                "claude",
                bare_config=False,
            ) == {agent.name: agent.resolve()}
            assert selected_runtime_agent_sources(
                "agy",
                bare_config=False,
            ) == {}
            assert selected_runtime_skill_sources(
                str(root),
                bare_config=True,
                disabled=[],
            ) == {}


def test_structural_plan_freezes_drift_and_references_secrets() -> None:
    with tempfile.TemporaryDirectory() as raw:
        package = Path(raw) / "package"
        package.mkdir()
        record = _record(package)
        state = {
            "profile": {
                "schema_version": 3,
                "status": "active",
                "generation": "install-generation",
                "mode": "default",
                "provider": "claude",
                "provider_identity": None,
            },
            "store": (10, 20),
            "settings_fp": (30, 40),
            "label": "prepared",
            "setting": "prepared",
            "grant": "grant-a",
        }
        inputs = {
            "cwd": str(Path(raw)),
            "user_facing": True,
            "bare_config": False,
            "working_mode": "build",
            "provider_run_config": {
                "mcp_servers": {
                    "explicit": {
                        "command": "/bin/echo",
                        "args": ["ready"],
                        "env": {"API_TOKEN": "never-persist"},
                        "tool_names": ["explicit_tool"],
                    },
                },
            },
            "resolved_harness_run_config": {
                "profile_id": "prepared-profile",
                "launcher_projection": {
                    "extension_setting_overlays": {
                        "example.extension": {
                            "label": {"value": "prepared"},
                            "access_token": {"value": "never-persist"},
                        },
                    },
                },
            },
        }

        def runtime_configs(*_args, **_kwargs):
            return {
                "example-mcp": {
                    "command": "/bin/echo",
                    "args": ["runtime"],
                    "env": {
                        "BETTER_CLAUDE_INTERNAL_TOKEN": "never-persist",
                        "LABEL": state["label"],
                    },
                    "tool_names": ["runtime_tool"],
                },
            }

        def grants(**_kwargs):
            return {
                "example.extension:example-mcp": {
                    "scope": "project",
                    "digest": state["grant"],
                },
            }

        patches = (
            patch("installation_profile.load", side_effect=lambda: dict(state["profile"])),
            patch(
                "installation_profile.capabilities",
                return_value={"integrations_enabled": True},
            ),
            patch("extension_store.store_fingerprint", side_effect=lambda: state["store"]),
            patch(
                "extension_store.extension_settings_fingerprint",
                side_effect=lambda: state["settings_fp"],
            ),
            patch("extension_store.list_extensions", return_value=[record]),
            patch(
                "extension_store.runtime_package_content_fingerprint",
                return_value="a" * 64,
            ),
            patch(
                "extension_store.permission_grants",
                return_value={"backend_routes": True},
            ),
            patch(
                "extension_store.get_extension_settings",
                side_effect=lambda _extension_id: {
                    "label": state["setting"],
                    "access_token": "never-persist",
                },
            ),
            patch(
                "extension_store.resolve_native_mcp_servers_for_context",
                side_effect=grants,
            ),
            patch(
                "extension_store.runtime_mcp_server_configs",
                side_effect=runtime_configs,
            ),
            patch(
                "extension_store.native_mcp_server_configs",
                return_value={},
            ),
            patch(
                "extension_store.native_mcp_launcher_server_configs",
                return_value={},
            ),
        )
        with ExitStack() as stack:
            for runtime_patch in patches:
                stack.enter_context(runtime_patch)
            prepared = structural_provider_runtime_plan(inputs, "claude")
            state["profile"]["generation"] = "mutated"
            state["store"] = (99, 99)
            state["settings_fp"] = (98, 98)
            state["label"] = "mutated"
            state["setting"] = "mutated"
            state["grant"] = "grant-b"
            record["manifest"]["entrypoints"]["mcp"][0]["predicate"][
                "equals"
            ]["working_mode"] = "mutated"
            inputs["resolved_harness_run_config"]["launcher_projection"][
                "extension_setting_overlays"
            ]["example.extension"]["label"]["value"] = "mutated"

        encoded = json.dumps(prepared, sort_keys=True)
        assert "never-persist" not in encoded
        reject_secrets(prepared, label="structural runtime plan")
        assert prepared["resolved_plan"]["harness"]["profile_id"] == "prepared-profile"
        assert prepared["resolved_plan"]["tools"] == [
            "explicit_tool",
            "runtime_tool",
        ]
        assert prepared["extension_state"]["store_fingerprint"] == [10, 20]
        assert prepared["extension_state"]["native_grants"][
            "example.extension:example-mcp"
        ]["digest"] == "grant-a"
        assert prepared["installation_decisions"]["profile"]["generation"] == (
            "install-generation"
        )
        extension = prepared["extension_state"]["extensions"][0]
        assert extension["package_fingerprint"] == "a" * 64
        assert extension["mcp_predicates"][0]["predicate"]["equals"] == {
            "working_mode": "build",
        }
        assert extension["settings"]["label"] == "prepared"
        assert extension["setting_overlays"]["label"]["value"] == "prepared"
        assert extension["settings"]["access_token_ref"] == {
            "kind": "extension_setting",
            "extension_id": "example.extension",
            "key_sha256": (
                "86b3901eea37a04e8547cd912225f548"
                "d2e0a92078887682ce831a433072f9d1"
            ),
        }
        runtime_server = next(
            server
            for server in prepared["resolved_plan"]["mcp_servers"]
            if server["name"] == "example-mcp"
        )
        runtime_config = runtime_server["config"]["runtime"]
        assert runtime_config["env"]["BETTER_CLAUDE_INTERNAL_TOKEN_ref"] == {
            "kind": "runtime_value",
            "path_sha256": (
                "983b6a26a7df885e2cffee06feb98e27"
                "cd143ffd97c1369343c796cf1a031a65"
            ),
        }


if __name__ == "__main__":
    test_selected_sources_are_exact_and_respect_gates()
    test_structural_plan_freezes_drift_and_references_secrets()
    print("PASS provider runtime plan source")
