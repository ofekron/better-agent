from __future__ import annotations

import copy
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provider_runtime_capability_model import (  # noqa: E402
    normalize_harness_plan,
    reject_secrets,
    serialize_runtime_plan,
)
from provider_runtime_plan_source import (  # noqa: E402
    hydrate_frozen_provider_runtime_plan,
    hydrate_runner_operation_broker,
    hydrate_runner_runtime_plan,
    selected_runtime_agent_sources,
    selected_runtime_skill_sources,
    structural_provider_runtime_plan,
)
from provider_session_events_runner import effective_mcp_servers  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from runner import _claude_mcp_variants  # noqa: E402
from runtime_skill_templates import RuntimeSkillSource  # noqa: E402


def _assert_contract_rejected(callable_) -> None:
    try:
        callable_()
    except ExecutionContractError:
        return
    raise AssertionError("invalid runtime plan was accepted")


def test_harness_secret_refs_are_narrow_and_authoritative() -> None:
    refs = {
        "example.extension": [
            "extension-setting:example.extension:access_token",
            "extension-setting:example.extension:api_key",
        ],
    }
    base_harness = {
        "profile_id": "prepared-profile",
        "secret_refs": refs,
        "launcher_projection": {
            "secret_refs": refs,
        },
    }
    assert normalize_harness_plan(base_harness)["secret_refs"] == refs
    invalid_harnesses = []

    duplicate = copy.deepcopy(base_harness)
    duplicate["secret_refs"]["example.extension"].append(
        "extension-setting:example.extension:access_token",
    )
    duplicate["launcher_projection"]["secret_refs"] = copy.deepcopy(
        duplicate["secret_refs"],
    )
    invalid_harnesses.append(duplicate)

    wrong_owner = copy.deepcopy(base_harness)
    wrong_owner["secret_refs"]["example.extension"][0] = (
        "extension-setting:other.extension:access_token"
    )
    wrong_owner["launcher_projection"]["secret_refs"] = copy.deepcopy(
        wrong_owner["secret_refs"],
    )
    invalid_harnesses.append(wrong_owner)

    malformed_key = copy.deepcopy(base_harness)
    malformed_key["secret_refs"]["example.extension"][0] = (
        "extension-setting:example.extension:AccessToken"
    )
    malformed_key["launcher_projection"]["secret_refs"] = copy.deepcopy(
        malformed_key["secret_refs"],
    )
    invalid_harnesses.append(malformed_key)

    divergent = copy.deepcopy(base_harness)
    divergent["launcher_projection"]["secret_refs"] = {}
    invalid_harnesses.append(divergent)

    for malformed_refs in (None, ""):
        malformed_top = copy.deepcopy(base_harness)
        malformed_top["secret_refs"] = malformed_refs
        invalid_harnesses.append(malformed_top)
        malformed_launcher = copy.deepcopy(base_harness)
        malformed_launcher["launcher_projection"]["secret_refs"] = (
            malformed_refs
        )
        invalid_harnesses.append(malformed_launcher)

    for key, value in (
        ("authorization_ref", "opaque-value"),
        ("api_key_refs", ["opaque-value"]),
        ("token_ref", "opaque-value"),
    ):
        fake_ref = copy.deepcopy(base_harness)
        fake_ref[key] = value
        invalid_harnesses.append(fake_ref)
        fake_launcher_ref = copy.deepcopy(base_harness)
        fake_launcher_ref["launcher_projection"][key] = value
        invalid_harnesses.append(fake_launcher_ref)

    nested_fake_ref = copy.deepcopy(base_harness)
    nested_fake_ref["nested"] = {"token_ref": "opaque-value"}
    invalid_harnesses.append(nested_fake_ref)

    for invalid_harness in invalid_harnesses:
        _assert_contract_rejected(
            lambda value=invalid_harness: normalize_harness_plan(value),
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
            ) == {"keep": RuntimeSkillSource(root=skill.resolve())}
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
                        "env": {"LABEL": "explicit"},
                        "tool_names": ["explicit_tool"],
                    },
                },
            },
            "resolved_harness_run_config": {
                "profile_id": "prepared-profile",
                "secret_refs": {
                    "example.extension": [
                        "extension-setting:example.extension:access_token",
                        "extension-setting:example.extension:api_key",
                    ],
                },
                "launcher_projection": {
                    "secret_refs": {
                        "example.extension": [
                            "extension-setting:example.extension:access_token",
                            "extension-setting:example.extension:api_key",
                        ],
                    },
                    "extension_setting_overlays": {
                        "example.extension": {
                            "label": {"value": "prepared"},
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
                        "BETTER_CLAUDE_EXTENSION_ID": "example.extension",
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
                "extension_store.get_extension_setting_values",
                side_effect=lambda _extension_id: {
                    "schema": record["manifest"]["entrypoints"]["settings"],
                    "values": {
                        "label": state["setting"],
                        "access_token": "never-persist",
                    },
                },
            ),
            patch(
                "extension_store.get_extension_settings",
                side_effect=lambda _extension_id: (_ for _ in ()).throw(
                    AssertionError(
                        "turn dispatch must not call the OS-keychain-probing "
                        "get_extension_settings; use get_extension_setting_values",
                    ),
                ),
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
            patch(
                "extension_token_registry.mint",
                return_value="never-persist",
            ),
        )
        with ExitStack() as stack:
            for runtime_patch in patches:
                stack.enter_context(runtime_patch)
            prepared = structural_provider_runtime_plan(inputs, "claude")
            expected_refs = inputs["resolved_harness_run_config"][
                "secret_refs"
            ]
            assert prepared["resolved_plan"]["harness"]["secret_refs"] == (
                expected_refs
            )
            assert prepared["resolved_plan"]["harness"][
                "launcher_projection"
            ]["secret_refs"] == expected_refs

            fake_ref_inputs = copy.deepcopy(inputs)
            fake_ref_inputs["resolved_harness_run_config"][
                "authorization_ref"
            ] = "opaque-value"
            _assert_contract_rejected(
                lambda: structural_provider_runtime_plan(
                    fake_ref_inputs,
                    "claude",
                ),
            )

            hydrated = hydrate_frozen_provider_runtime_plan(
                prepared["resolved_plan"],
            )
            explicit_server = next(
                server
                for server in hydrated["mcp_servers"]
                if server["name"] == "explicit"
            )
            runtime_server = next(
                server
                for server in hydrated["mcp_servers"]
                if server["name"] == "example-mcp"
            )
            assert explicit_server["config"]["explicit"]["env"]["LABEL"] == "explicit"
            assert runtime_server["config"]["runtime"]["env"][
                "BETTER_CLAUDE_INTERNAL_TOKEN"
            ] == "never-persist"
            inputs["provider_run_config"]["mcp_servers"]["explicit"]["env"][
                "API_TOKEN"
            ] = "untyped-secret"
            try:
                structural_provider_runtime_plan(inputs, "claude")
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("untyped runtime secret was accepted")
            inputs["provider_run_config"]["mcp_servers"]["explicit"]["env"].pop(
                "API_TOKEN",
            )
            inputs["resolved_harness_run_config"]["authorization"] = (
                "never-persist"
            )
            try:
                structural_provider_runtime_plan(inputs, "claude")
            except ExecutionContractError:
                pass
            else:
                raise AssertionError(
                    "unauthorized harness secret reached the runtime plan",
                )
            inputs["resolved_harness_run_config"].pop("authorization")
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
            assert hydrate_frozen_provider_runtime_plan(
                prepared["resolved_plan"],
            ) == hydrated

        encoded = json.dumps(prepared, sort_keys=True)
        assert "never-persist" not in encoded
        serialize_runtime_plan(prepared["resolved_plan"])
        reject_secrets(
            prepared["extension_state"],
            label="extension runtime state",
        )
        reject_secrets(
            prepared["installation_decisions"],
            label="installation decisions",
        )
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
            "kind": "extension_identity",
            "extension_id": "example.extension",
        }


def test_agy_plan_preserves_builtin_mcp_tools_with_typed_broker_hydration() -> None:
    inputs = {
        "app_session_id": "session-id",
        "backend_url": "http://127.0.0.1:8000",
        "cwd": "/tmp",
        "model": "agy-model",
        "provider_id": "agy-provider",
        "provider_kind": "agy",
        "user_facing": True,
        "bare_config": False,
        "working_mode": "file_editing",
        "provider_run_config": {"mcp_servers": {}},
        "resolved_harness_run_config": {},
    }
    with (
        patch("installation_profile.integrations_enabled", return_value=True),
        patch("installation_profile.load", return_value={}),
        patch(
            "installation_profile.capabilities",
            return_value={"integrations_enabled": True},
        ),
        patch("extension_store.runtime_mcp_server_configs", return_value={}),
        patch("extension_store.native_mcp_server_configs", return_value={}),
        patch(
            "extension_store.native_mcp_launcher_server_configs",
            return_value={},
        ),
        patch("extension_store.list_extensions", return_value=[]),
        patch("extension_store.store_fingerprint", return_value=(1, 2)),
        patch(
            "extension_store.extension_settings_fingerprint",
            return_value=(3, 4),
        ),
        patch(
            "extension_store.resolve_native_mcp_servers_for_context",
            return_value={},
        ),
    ):
        prepared = structural_provider_runtime_plan(inputs, "agy")

    servers = {
        server["name"]: server
        for server in prepared["resolved_plan"]["mcp_servers"]
    }
    assert set(servers) == {"ui", "open-config-panel", "capabilities"}
    assert servers["ui"]["tool_names"] == [
        "open_file_panel",
        "request_user_approval",
        "request_user_input",
        "start_file_discussion",
    ]
    assert servers["open-config-panel"]["tool_names"] == [
        "open_config_panel",
    ]
    assert servers["capabilities"]["tool_names"] == [
        "list_capabilities",
        "load_capability",
        "release_capability",
    ]
    encoded = json.dumps(prepared, sort_keys=True)
    assert "must-not-persist" not in encoded
    assert "unix:/tmp/runtime-broker.sock" not in encoded
    assert r"C:\WINDOWS" not in encoded
    remote_plan = copy.deepcopy(prepared["resolved_plan"])
    for server in remote_plan["mcp_servers"]:
        server["config"]["effective"]["env"]["SYSTEMROOT"] = "/primary/SystemRoot"
    with patch.dict(
        "os.environ",
        {
            "PATH": r"C:\Windows\System32",
            "SystemRoot": r"C:\WINDOWS",
            "PARENT_SECRET": "secret",
        },
        clear=True,
    ):
        hydrated = hydrate_runner_runtime_plan(
            remote_plan,
            "unix:/tmp/runtime-broker.sock",
        )
    for server in hydrated["mcp_servers"]:
        env = server["config"]["effective"]["env"]
        assert env["SystemRoot"] == r"C:\WINDOWS"
        assert "SYSTEMROOT" not in env
        assert env["PATH"] == r"C:\Windows\System32"
        assert "PARENT_SECRET" not in env
        assert env["BETTER_AGENT_RUNTIME_BROKER"] == (
            "unix:/tmp/runtime-broker.sock"
        )
        assert env["BETTER_CLAUDE_RUNTIME_BROKER"] == (
            "unix:/tmp/runtime-broker.sock"
        )


def _claude_plan(runner: str | None) -> dict:
    inputs = {
        "app_session_id": "session-id",
        "backend_url": "http://127.0.0.1:8000",
        "cwd": "/tmp",
        "model": "claude-opus-4-7[1m]",
        "provider_id": "claude-provider",
        "provider_kind": "claude",
        "user_facing": True,
        "bare_config": False,
        "working_mode": "file_editing",
        "provider_run_config": {"mcp_servers": {}},
        "resolved_harness_run_config": {},
    }
    if runner is not None:
        inputs["runner"] = runner
    with (
        patch("installation_profile.integrations_enabled", return_value=True),
        patch("installation_profile.load", return_value={}),
        patch(
            "installation_profile.capabilities",
            return_value={"integrations_enabled": True},
        ),
        patch(
            "extension_store.runtime_mcp_server_configs",
            return_value={
                "example-runtime": {
                    "command": "/bin/echo",
                    "args": ["ready"],
                    "env": {},
                },
            },
        ),
        patch(
            "extension_store.native_mcp_server_configs",
            return_value={
                "native-only": {
                    "command": "/bin/echo",
                    "args": ["native"],
                    "env": {},
                },
            },
        ),
        patch(
            "extension_store.native_mcp_launcher_server_configs",
            return_value={},
        ),
        patch("extension_store.list_extensions", return_value=[]),
        patch("extension_store.store_fingerprint", return_value=(1, 2)),
        patch(
            "extension_store.extension_settings_fingerprint",
            return_value=(3, 4),
        ),
        patch(
            "extension_store.resolve_native_mcp_servers_for_context",
            return_value={},
        ),
    ):
        return structural_provider_runtime_plan(inputs, "claude")[
            "resolved_plan"
        ]


def test_claude_kind_plan_matches_better_agent_runner_delivery() -> None:
    resolved_plan = _claude_plan("better_agent_runner")

    assert resolved_plan["mcp_servers"], "test must exercise at least one server"
    for server in resolved_plan["mcp_servers"]:
        assert set(server["config"]) == {"effective"}, server["name"]
    effective = effective_mcp_servers(resolved_plan)
    assert "native-only" not in effective
    assert set(effective) == {
        server["name"] for server in resolved_plan["mcp_servers"]
    }


def test_claude_kind_plan_matches_native_runner_delivery() -> None:
    resolved_plan = _claude_plan(None)

    assert resolved_plan["mcp_servers"], "test must exercise at least one server"
    variants = _claude_mcp_variants(resolved_plan)
    assert "native-only" in variants
    assert set(variants) == {
        server["name"] for server in resolved_plan["mcp_servers"]
    }


def test_claude_kind_plan_rejects_unknown_runner() -> None:
    _assert_contract_rejected(lambda: _claude_plan("unknown"))


def test_reject_secrets_does_not_flag_non_secret_auth_mode_settings() -> None:
    """Regression: an extension setting overlay literally named "auth_mode"
    (e.g. a GTM extension's oauth-vs-api-key selector) is a mode choice, not
    a credential — it must not trip the secret-free check. Found live: this
    false positive blocked every new session, on every provider, because
    `_SECRET_KEY_RE` matched the bare "auth" component of "auth_mode". Real
    secret-shaped keys (including ones containing "auth", like
    "auth_token") must still be rejected."""
    reject_secrets(
        {
            "extension_setting_overlays": {
                "ofek-dev.gtm": {
                    "auth_mode": {"value": "oauth", "schema_hash": "x"},
                },
            },
        },
        label="harness plan",
        allow_reference_keys=False,
    )
    for bad_key in (
        "api_key", "authorization", "credential", "password", "secret",
        "token", "auth_token",
    ):
        _assert_contract_rejected(
            lambda bad_key=bad_key: reject_secrets(
                {bad_key: "raw-value"},
                label="t",
                allow_reference_keys=False,
            ),
        )


if __name__ == "__main__":
    test_harness_secret_refs_are_narrow_and_authoritative()
    test_selected_sources_are_exact_and_respect_gates()
    test_structural_plan_freezes_drift_and_references_secrets()
    test_agy_plan_preserves_builtin_mcp_tools_with_typed_broker_hydration()
    test_reject_secrets_does_not_flag_non_secret_auth_mode_settings()
    print("PASS provider runtime plan source")
