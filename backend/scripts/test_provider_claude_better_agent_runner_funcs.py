"""Unit coverage for the two module-level assembly functions in
provider_claude_better_agent_runner.py:

- ``_build_better_agent_runner_input`` — builds the runner_better_agent input.json
  dict, field-for-field.
- ``prepare_better_agent_runner_run`` — assembles the full PreparedExecution
  (attestation, runtime plan, capabilities) and delegates to
  ``prepare_family_execution``.

These are orchestration functions: given a provider + start_arguments, they
compute a result by calling a fixed set of imported building-block helpers.
The unit-under-test is the *wiring* (arg forwarding, error branches, dict
construction, attestation-failure handling), so the building blocks are mocked
at their source modules; the pure helpers (_normalize_mode / _normalize_reasoning
/ _mode_projection) run real.

Run:
    cd backend && .venv/bin/python -m pytest scripts/test_provider_claude_better_agent_runner_funcs.py \
        --cov=provider_claude_better_agent_runner --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import provider_claude_better_agent_runner as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _claude_provider(*, supports_manager_mode=True, supports_fork=True,
                     supports_reasoning_effort=True) -> mock.MagicMock:
    """A stand-in for the real ClaudeProvider instance these functions
    receive (`provider.KIND == "claude"` end to end)."""
    p = mock.MagicMock(name="claude_provider")
    p.KIND = "claude"
    p.id = "prov-claude-1"
    p.supports_manager_mode = supports_manager_mode
    p.supports_fork = supports_fork
    p.supports_reasoning_effort = supports_reasoning_effort
    p.reasoning_effort_options = ("minimal", "low", "medium", "high", "max")
    return p


def _base_start_arguments(**overrides) -> dict:
    base = {
        "app_session_id": "sess-123",
        "run_id": "run-abc",
        "model": "claude-sonnet-4-5",
        "mode": "native",
        "cwd": "/tmp/work",
        "bare_config": None,
        "disabled_runtime_skills": (),
    }
    base.update(overrides)
    return base


# ===========================================================================
# _build_better_agent_runner_input
# ===========================================================================

class TestBuildBetterAgentRunnerInput:
    """Exercises the input.json dict construction and its branches."""

    def test_model_missing_raises(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        del args["model"]
        with self._entered_patches():
            with pytest.raises(ValueError, match="claude provider requires a model"):
                mod._build_better_agent_runner_input(provider, args, {"mode": "subscription"})

    def test_model_empty_raises(self):
        provider = _claude_provider()
        args = _base_start_arguments(model="   ")
        with self._entered_patches():
            with pytest.raises(ValueError, match="claude provider requires a model"):
                mod._build_better_agent_runner_input(provider, args, {"mode": "subscription"})

    def test_full_assembly_and_field_filtering(self):
        """Happy path: output keys are exactly input_fields ∪ {provider_mode},
        and each computed field reflects its source."""
        provider = _claude_provider()
        args = _base_start_arguments()
        authority = {"mode": "subscription", "runner": "better_agent_runner"}

        with self._entered_patches() as ctx:
            (strategy_for, resolve_policy, disabled_skills, resolve_perm,
             integrations, context_strategy, ext_ready, required_mcp,
             ext_role) = (
                ctx["strategy_for"], ctx["resolve_extension_run_policy"],
                ctx["disabled_runtime_skills_for_run"], ctx["resolve_for_run"],
                ctx["integrations_enabled"], ctx["get_context_strategy"],
                ctx["is_extension_runtime_ready"], ctx["required_profile_mcp_server_names"],
                ctx["extension_id_for_role"],
            )

            strategy_for.return_value = SimpleNamespace(
                kind="openai",
                # The runner-facing field set. The final filter is
                # `{key: values.get(key) for key in input_fields}`, so EVERY
                # input_field key appears in the output — keys not produced by
                # `values` map to None, and keys in `values` but NOT in
                # input_fields are dropped. We include `not_produced` (absent
                # in values → None) and a `dropped_values_key` only in values.
                input_fields=frozenset({
                    "app_session_id", "model", "provider_kind", "provider_mode",
                    "backend_url", "permission", "runner", "canonical_mode",
                    "mode", "team_orchestration_enabled",
                    "coordination_enabled", "required_mcp_server_names",
                    "merged_policy", "context_strategy", "not_produced",
                }),
            )
            resolve_policy.return_value = {"merged_policy": "POL"}
            disabled_skills.return_value = ("skill-x",)
            resolve_perm.return_value = {"allow": "*"}
            integrations.return_value = True
            context_strategy.return_value = "compact"
            ext_ready.return_value = True
            required_mcp.return_value = ["zeta", "alpha"]  # unsorted on purpose
            ext_role.return_value = "team-orchestration-ext"

            result = mod._build_better_agent_runner_input(provider, args, authority)

        # Output keys are exactly the input_field set (provider_mode included).
        assert set(result) == {
            "app_session_id", "model", "provider_kind", "provider_mode",
            "backend_url", "permission", "runner", "canonical_mode", "mode",
            "team_orchestration_enabled", "coordination_enabled",
            "required_mcp_server_names", "merged_policy", "context_strategy",
            "not_produced",
        }
        # Absent-in-values input_field key maps to None (filter keeps it).
        assert result["not_produced"] is None

        # Direct passthroughs / explicit computations.
        assert result["app_session_id"] == "sess-123"
        assert result["model"] == "claude-sonnet-4-5"
        assert result["provider_kind"] == "claude"
        assert result["provider_mode"] == "subscription"
        assert result["runner"] == "better_agent_runner"
        assert result["permission"] == {"allow": "*"}
        assert result["merged_policy"] == "POL"
        assert result["context_strategy"] == "compact"
        assert result["canonical_mode"] == "native"
        assert result["mode"] == "native"  # native is not remapped
        assert result["required_mcp_server_names"] == ["alpha", "zeta"]  # sorted
        assert result["team_orchestration_enabled"] is True
        assert result["coordination_enabled"] is True
        # backend_url fallback to default when neither start_arguments nor env.
        assert result["backend_url"] == "http://localhost:8000"

        # Arg forwarding into the helpers.
        resolve_policy.assert_called_once()
        disabled_skills.assert_called_once_with(
            session_record={"working_mode": "native"}, worker_record={})
        resolve_perm.assert_called_once()
        # Both capability flags consulted extension readiness.
        assert ext_ready.call_count == 2
        ext_role.assert_called_once_with("team-orchestration")

    def test_team_mode_canonical_remapped_to_manager_for_openai_strategy(self):
        """strategy.kind=='openai' maps canonical 'team' -> 'manager' in the
        runner-facing mode field, while canonical_mode stays 'team'."""
        provider = _claude_provider()
        args = _base_start_arguments(mode="team")
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"canonical_mode", "mode"}))
            ctx["integrations_enabled"].return_value = False
            ctx["is_extension_runtime_ready"].return_value = True
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["canonical_mode"] == "team"
        assert result["mode"] == "manager"

    def test_capability_flags_false_when_integrations_disabled(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai",
                input_fields=frozenset({"team_orchestration_enabled",
                                        "coordination_enabled"}))
            ctx["integrations_enabled"].return_value = False
            ctx["is_extension_runtime_ready"].return_value = True
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["team_orchestration_enabled"] is False
        assert result["coordination_enabled"] is False

    def test_capability_flags_false_when_extension_not_ready(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai",
                input_fields=frozenset({"team_orchestration_enabled",
                                        "coordination_enabled"}))
            ctx["integrations_enabled"].return_value = True
            ctx["is_extension_runtime_ready"].return_value = False
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["team_orchestration_enabled"] is False
        assert result["coordination_enabled"] is False

    def test_backend_url_from_start_arguments_wins(self):
        provider = _claude_provider()
        args = _base_start_arguments(backend_url="http://explicit:9999")
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"backend_url"}))
            ctx["get_env"].return_value = "http://env:1"
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["backend_url"] == "http://explicit:9999"

    def test_backend_url_falls_back_to_env(self):
        provider = _claude_provider()
        args = _base_start_arguments()  # no backend_url
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"backend_url"}))
            ctx["get_env"].return_value = "http://env-backend:8000"
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["backend_url"] == "http://env-backend:8000"
        # The env var name is a wire contract — verify it is consulted.
        ctx["get_env"].assert_called_once_with("BETTER_CLAUDE_BACKEND_URL")

    def test_backend_url_empty_string_falls_through(self):
        """A whitespace-only start_arguments backend_url is treated as absent."""
        provider = _claude_provider()
        args = _base_start_arguments(backend_url="   ")
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"backend_url"}))
            ctx["get_env"].return_value = ""
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["backend_url"] == "http://localhost:8000"

    def test_provider_mode_defaults_when_authority_mode_absent(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"provider_mode"}))
            ctx["integrations_enabled"].return_value = False
            result = mod._build_better_agent_runner_input(
                provider, args, {"runner": "r"})  # no "mode"
        assert result["provider_mode"] == "subscription"

    def test_files_and_images_normalized_to_lists(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        args["files"] = None
        args["images"] = None
        with self._entered_patches() as ctx:
            ctx["strategy_for"].return_value = SimpleNamespace(
                kind="openai", input_fields=frozenset({"files", "images"}))
            ctx["integrations_enabled"].return_value = False
            result = mod._build_better_agent_runner_input(
                provider, args, {"mode": "subscription", "runner": "r"})
        assert result["files"] == []
        assert result["images"] == []

    def _entered_patches(self):
        """Enter all _patches() and return a dict mapping friendly names to
        the started patches for assertion."""
        patchers = {
            "_session_records": mock.patch(
                "provider_session_events_execution._session_records",
                return_value=({"working_mode": "native"}, {})),
            "strategy_for": mock.patch(
                "provider_session_events_execution_strategy.strategy_for"),
            "resolve_extension_run_policy": mock.patch(
                "extension_run_policy.resolve_extension_run_policy",
                return_value={}),
            "disabled_runtime_skills_for_run": mock.patch(
                "extension_run_policy.disabled_runtime_skills_for_run",
                return_value=()),
            "resolve_for_run": mock.patch("permission.resolve_for_run"),
            "integrations_enabled": mock.patch(
                "installation_profile.integrations_enabled"),
            "get_context_strategy": mock.patch(
                "user_prefs.get_context_strategy", return_value=""),
            "is_extension_runtime_ready": mock.patch(
                "extension_store.is_extension_runtime_ready"),
            "required_profile_mcp_server_names": mock.patch(
                "extension_store.required_profile_mcp_server_names",
                return_value=[]),
            "extension_id_for_role": mock.patch(
                "extension_store.extension_id_for_role"),
            "get_env": mock.patch("env_compat.get_env", return_value=""),
        }
        return _PatchCtx(patchers)


class _PatchCtx:
    """Helper that enters a dict of patchers and yields itself as the context
    value (so tests can index started mocks by friendly name)."""

    def __init__(self, patchers: dict):
        self._patchers = patchers
        self.started = {}

    def __enter__(self):
        for name, p in self._patchers.items():
            self.started[name] = p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patchers.values():
            p.stop()
        return False

    def __getitem__(self, name):
        return self.started[name]


# ===========================================================================
# prepare_better_agent_runner_run
# ===========================================================================

class TestPrepareBetterAgentRunnerRun:
    """Exercises the PreparedExecution assembly and its branches."""

    def test_run_id_missing_raises(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        args["run_id"] = ""
        with pytest.raises(ValueError, match="run id is unavailable"):
            mod.prepare_better_agent_runner_run(provider, args)

    def test_run_id_absent_raises(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        del args["run_id"]
        with pytest.raises(ValueError, match="run id is unavailable"):
            mod.prepare_better_agent_runner_run(provider, args)

    def test_attestation_failure_raises_runtime_error(self):
        provider = _claude_provider()
        args = _base_start_arguments()
        launch = mock.MagicMock()
        launch.attest_with_reason.return_value = (False, "binary tampered")

        with mock.patch.object(mod, "_build_better_agent_runner_input",
                               return_value={"cwd": "/x", "bare_config": None,
                                             "disabled_runtime_skills": ()}), \
             mock.patch("provider_family_launch_attestation.FamilyLaunchAttestation") as FLA, \
             mock.patch("provider_family_launch_attestation.capture_config_scope"), \
             mock.patch("provider_family_launch_attestation.capture_runner_launch"), \
             mock.patch("provider_family_launch_attestation.capture_cli_launch"), \
             mock.patch("provider_manifest.runner_module_for", return_value="runner_better_agent"), \
             mock.patch.object(mod.logger, "error") as log_error:
            FLA.capture.return_value = launch
            with pytest.raises(RuntimeError, match="launch authority changed"):
                mod.prepare_better_agent_runner_run(provider, args)
        log_error.assert_called_once()
        # The failure reason is logged.
        assert "binary tampered" in log_error.call_args[0][1]

    def test_happy_path_assembles_and_delegates(self):
        provider = _claude_provider()
        provider.execution_authority_record.return_value = {"mode": "subscription"}
        args = _base_start_arguments()
        runner_input = {"cwd": "/work", "bare_config": "cfg-x",
                        "disabled_runtime_skills": ("s1",)}

        launch = mock.MagicMock()
        launch.attest_with_reason.return_value = (True, "")

        with mock.patch.object(mod, "_build_better_agent_runner_input",
                               return_value=runner_input) as build_input, \
             mock.patch("provider_family_launch_attestation.FamilyLaunchAttestation") as FLA, \
             mock.patch("provider_family_launch_attestation.capture_config_scope") as cap_cfg, \
             mock.patch("provider_family_launch_attestation.capture_runner_launch") as cap_run, \
             mock.patch("provider_family_launch_attestation.capture_cli_launch") as cap_cli, \
             mock.patch("provider_manifest.runner_module_for",
                        return_value="runner_better_agent") as runner_mod_for, \
             mock.patch("provider_runtime_plan_source.structural_provider_runtime_plan") as plan, \
             mock.patch("provider_runtime_plan_source.selected_runtime_skill_sources") as sel_skills, \
             mock.patch("provider_runtime_plan_source.selected_runtime_agent_sources") as sel_agents, \
             mock.patch("provider_family_runtime_capabilities.snapshot_family_runtime_capabilities") as snap_caps, \
             mock.patch("provider_family_execution_runtime.prepare_family_execution") as prep_exec:
            FLA.capture.return_value = launch
            plan.return_value = {"resolved_plan": "PLAN", "extension_state": "EXT",
                                  "installation_decisions": "DEC"}
            prep_exec.return_value = "PREPARED_EXECUTION"

            result = mod.prepare_better_agent_runner_run(provider, args)

        assert result == "PREPARED_EXECUTION"

        # authority + runner_input wiring.
        provider.execution_authority_record.assert_called_once_with(args)
        build_input.assert_called_once_with(
            provider, args, {"mode": "subscription"})

        # capture_config_scope called with the better_agent_sessions config root.
        cap_cfg.assert_called_once()
        assert cap_cfg.call_args.kwargs["config_paths"] == ()
        assert cap_cfg.call_args.kwargs["resume_path"] is None

        # runner_module looked up for this provider kind + runner name.
        runner_mod_for.assert_called_once_with("claude", "better_agent_runner")

        # runner_launch captured with kind-stamped fields.
        cap_run_kwargs = cap_run.call_args.kwargs
        assert cap_run_kwargs["runner_kind"] == "claude"
        assert cap_run_kwargs["runner_module"] == "runner_better_agent"
        assert cap_run_kwargs["run_dir"].name == "run-abc"

        # family attestation captured keyed by provider kind.
        fla_kwargs = FLA.capture.call_args.kwargs
        assert fla_kwargs["family"] == "claude"
        assert fla_kwargs["runner"] is cap_run.return_value
        assert fla_kwargs["downstream"] is cap_cli.return_value
        assert fla_kwargs["config"] is cap_cfg.return_value

        # cli launch captured with this kind as logical command.
        cli_kwargs = cap_cli.call_args.kwargs
        assert cli_kwargs["logical_command"] == "claude"

        # runtime plan sourced from runner_input + kind.
        plan.assert_called_once_with(runner_input, "claude")

        # skill/agent sources forwarded with bare_config truthiness + fields.
        sel_skills.assert_called_once_with("/work", True, ("s1",))
        sel_agents.assert_called_once_with("claude", True)

        # capabilities snapshot assembled from projection pieces.
        snap_kwargs = snap_caps.call_args.kwargs
        assert snap_kwargs["family"] == "claude"
        assert snap_kwargs["skill_sources"] is sel_skills.return_value
        assert snap_kwargs["agent_sources"] is sel_agents.return_value
        assert snap_kwargs["resolved_plan"] == "PLAN"
        assert snap_kwargs["extension_state"] == "EXT"
        assert snap_kwargs["installation_decisions"] == "DEC"

        # final delegation to prepare_family_execution.
        prep_exec.assert_called_once()
        prep_kwargs = prep_exec.call_args.kwargs
        assert prep_kwargs["start_arguments"] is args
        assert prep_kwargs["runner_input"] is runner_input
        assert prep_kwargs["launch"] is launch
        assert prep_kwargs["capabilities"] is snap_caps.return_value
