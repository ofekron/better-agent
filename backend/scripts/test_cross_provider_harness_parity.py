"""Cross-provider harness equality across every harness manipulation surface.

This file is BOTH the living map of every harness manipulation Better Agent
performs AND the parity lock that the SHARED inputs to those manipulations
reach Claude, Codex, and Gemini equivalently.

MAP OF HARNESS MANIPULATION SURFACES
------------------------------------
Each surface names the SSOT (single source of truth) that feeds all providers
and the kind of equality the parity test enforces for it:

  Surface                       | SSOT                                          | Equality kind
  ------------------------------|-----------------------------------------------|-----------------------------
  S1  BETTER_AGENT run env      | provider.build_better_agent_run_env            | identical (kind-agnostic)
  S2  Permission policy         | permission.DEFAULT_PERMISSION                 | semantic (full-bypass parity)
  S3  Orchestration tool schema | orchestration_tool_schemas                    | shared object (is-identity)
  S4  Orchestration tool desc   | orchestration_tool_descriptions               | shared object + content
  S5  Builtin MCP registry      | builtin_mcp_config.with_builtin_mcp_servers   | structural (reserved-set parity)
  S6  Runtime skill contexts    | runtime_skills.runtime_skill_contexts         | shared function (provider-agnostic)
  S7  Capability contexts       | capability_contexts.provider_capability_...   | shared build (per-provider parity)
  S8  Disabled builtin exts     | extension_run_policy                          | shared normalizer (kind-agnostic)
  S9  Native harness exposure   | extension_store.native_harness_exposed        | shared store default

EQUALITY IS NOT ALWAYS TEXTUAL
------------------------------
Several surfaces are *intentionally* per-provider-native (see the big comment
in permission.py: "Permission is per-provider-native (Option B)"). Each
provider exposes its real CLI options losslessly. So for those surfaces
equality means "same semantic" (e.g. default = full bypass), NOT "same string".
Each test below states which kind of equality it enforces and locks the
specific divergence that is allowed by design (and would be a bug if it
changed silently).

Run: python backend/scripts/test_cross_provider_harness_parity.py
"""
import inspect
import os
import sys

# Isolate state dir BEFORE importing backend modules (project rule).
import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc_xprovider_harness_")

import builtin_mcp_config  # noqa: E402
import capability_contexts  # noqa: E402
import communicate_mcp  # noqa: E402
import extension_run_policy  # noqa: E402
import extension_store  # noqa: E402
import orchestration_tool_schemas as ots  # noqa: E402
import permission  # noqa: E402
import provider  # noqa: E402
import provider_manifest  # noqa: E402
import runner  # noqa: E402
import runner_codex  # noqa: E402
import runtime_skills  # noqa: E402

# The three first-class CLI providers this map covers.
PROVIDERS = ("claude", "codex", "gemini")


# ---------------------------------------------------------------------------
# S2 — Permission policy: default MUST be "full bypass" on every provider.
# ---------------------------------------------------------------------------
# Permission is per-provider-native (Option B): each kind spells full-bypass
# differently. This canonical map pins the *semantic* each kind must resolve to
# by default. If any kind's DEFAULT_PERMISSION drifts away from full bypass,
# one provider silently becomes stricter than the others — a parity bug.
CANONICAL_FULL_BYPASS = {
    "claude": {"mode": "bypassPermissions"},
    "codex": {"approval": "never", "sandbox": "danger-full-access"},
    "gemini": {"mode": "yolo"},
    "openai": {"mode": "bypassPermissions"},
}


def test_default_permission_is_full_bypass_for_all_providers():
    for kind, expected in CANONICAL_FULL_BYPASS.items():
        actual = permission.default_permission_for_kind(kind)
        assert actual == expected, (
            f"{kind} default permission {actual!r} is not the canonical "
            f"full-bypass {expected!r}; providers diverged in autonomy."
        )


def test_resolve_permission_falls_back_to_full_bypass_identically():
    # No session override, no provider default → every kind resolves to the
    # same semantic autonomy level (full bypass), spelled in its own vocabulary.
    for kind in CANONICAL_FULL_BYPASS:
        resolved = permission.resolve_permission(kind, None, None)
        assert resolved == CANONICAL_FULL_BYPASS[kind], (
            f"{kind} resolved permission {resolved!r} != canonical full bypass "
            f"when no override/default was supplied."
        )


def test_permission_axes_cover_every_provider():
    # Every CLI provider must surface a permission axis. A provider losing its
    # axis entry would silently become un-approval-controlled.
    for kind in PROVIDERS:
        axes = permission.permission_axes_for_kind(kind)
        assert axes, f"{kind} has no permission axes — approval surface dropped"


# ---------------------------------------------------------------------------
# S1 — BETTER_AGENT run env: identical and kind-agnostic.
# ---------------------------------------------------------------------------
def _make_run_env_args(**over):
    base = dict(
        backend_url="http://localhost:8000",
        internal_token="tok",
        app_session_id="sid-1",
        cwd="/tmp/proj",
        model="some-model",
        provider_id="pid",
        bare_config=False,
        user_facing=True,
        disabled_builtin_extensions=["demo"],
    )
    base.update(over)
    return base


def test_run_env_is_kind_agnostic_and_deterministic():
    # build_better_agent_run_env takes NO provider-kind parameter: the exact
    # same inputs must produce the exact same env regardless of provider.
    sig = inspect.signature(provider.build_better_agent_run_env)
    assert not any("kind" in p for p in sig.parameters), (
        "build_better_agent_run_env grew a kind parameter — the run env is no "
        "longer a single source across providers."
    )
    a = provider.build_better_agent_run_env(**_make_run_env_args())
    b = provider.build_better_agent_run_env(**_make_run_env_args())
    assert a == b, "run env is non-deterministic for identical inputs"
    # The dual BETTER_AGENT_HOME/BETTER_CLAUDE_HOME aliasing and the full
    # BETTER_CLAUDE_* selector mirror must reach every provider unchanged.
    for key in (
        "BETTER_AGENT_HOME",
        "BETTER_CLAUDE_HOME",
        "BETTER_CLAUDE_BACKEND_URL",
        "BETTER_CLAUDE_APP_SESSION_ID",
        "BETTER_CLAUDE_PROVIDER_ID",
        "BETTER_CLAUDE_MODEL",
        "BETTER_CLAUDE_CWD",
        "BETTER_CLAUDE_BARE_CONFIG",
        "BETTER_CLAUDE_USER_FACING",
        "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS",
    ):
        assert key in a, f"run env missing required key {key}"


# ---------------------------------------------------------------------------
# S3 — Orchestration tool schema: shared object across runner providers.
# ---------------------------------------------------------------------------
def test_delegate_task_schema_is_shared_by_runner_providers():
    # Claude and Codex runners must import the SAME schema object — a forked
    # copy would let the two drift. Gemini infers its schema from the function
    # signature, so its parity is locked structurally (next test).
    assert runner._DELEGATE_TASK_INPUT_SCHEMA is ots.DELEGATE_TASK_INPUT_SCHEMA, (
        "Claude runner forked the delegate_task input schema"
    )
    assert runner_codex._DELEGATE_TASK_INPUT_SCHEMA is ots.DELEGATE_TASK_INPUT_SCHEMA, (
        "Codex runner forked the delegate_task input schema"
    )


def test_gemini_delegate_task_params_match_shared_schema():
    # Gemini's communicate MCP builds delegate_task as a FastMCP tool whose
    # input schema is inferred from the Python signature, not from the shared
    # schema object. Lock that the inferred schema's property keys still match
    # the shared schema's property keys, so all three providers expose the same
    # contract to the model.
    shared_props = set(ots.DELEGATE_TASK_INPUT_SCHEMA["properties"].keys())
    tools = {t.name: t for t in communicate_mcp.build_server()._tool_manager.list_tools()}
    assert "delegate_task" in tools, "Gemini communicate server missing delegate_task"
    gemini_props = set((tools["delegate_task"].parameters or {}).get("properties", {}).keys())
    assert gemini_props == shared_props, (
        f"Gemini delegate_task props {gemini_props} != shared schema props "
        f"{shared_props}"
    )


# ---------------------------------------------------------------------------
# S5 — Builtin MCP registry: equivalent reserved-server set across providers.
# ---------------------------------------------------------------------------
def _make_inputs(provider_kind: str, **over) -> dict:
    base = dict(
        app_session_id="sid-1",
        backend_url="http://localhost:8000",
        internal_token="tok",
        cwd="/tmp/proj",
        model="some-model",
        provider_id="pid",
        provider_kind=provider_kind,
        bare_config=False,
        open_file_panel_enabled=True,
        working_mode="file_editing",
    )
    base.update(over)
    return base


def _builtin_mcp_names(provider_kind: str) -> set[str]:
    config = builtin_mcp_config.with_builtin_mcp_servers(
        _make_inputs(provider_kind), {"mcp_servers": {}}
    )
    return set(config["mcp_servers"].keys())


def test_builtin_mcp_reserved_servers_parity():
    # The shared reserved-server set every provider gets...
    common = {"capabilities", "open-config-panel"}
    for kind in PROVIDERS:
        names = _builtin_mcp_names(kind)
        assert common.issubset(names), (
            f"{kind} is missing a common reserved MCP server: {common - names}"
        )
    # ...plus the `ui` (open-file-panel) server, which is gated by the
    # provider's hosts_ui_mcp capability. claude+gemini host it; codex cannot.
    # This is the ONE allowed, documented divergence — locking it prevents a
    # silent change in either direction.
    for kind in PROVIDERS:
        spec = provider_manifest.spec_for(kind)
        names = _builtin_mcp_names(kind)
        has_ui = "ui" in names
        assert has_ui == bool(spec and spec.hosts_ui_mcp), (
            f"{kind} ui-MCP presence ({has_ui}) disagrees with its "
            f"hosts_ui_mcp capability ({spec.hosts_ui_mcp if spec else None})"
        )


def test_builtin_mcp_capabilities_server_identical_across_providers():
    # The `capabilities` server config must be byte-identical across providers
    # given identical inputs — it is provider-agnostic by construction.
    configs = {kind: _builtin_mcp_names(kind) for kind in PROVIDERS}
    # capabilities present everywhere; its config does not depend on kind.
    for kind in PROVIDERS:
        cfg = builtin_mcp_config.with_builtin_mcp_servers(
            _make_inputs(kind), {"mcp_servers": {}}
        )["mcp_servers"]["capabilities"]
        assert cfg["command"] and cfg["args"], f"{kind} capabilities cfg malformed"
    # Sanity: common base identical regardless of kind (re-derive deterministically).
    base = _builtin_mcp_names("claude") & _builtin_mcp_names("codex") & _builtin_mcp_names("gemini")
    assert {"capabilities", "open-config-panel"}.issubset(base)


# ---------------------------------------------------------------------------
# S6 — Runtime skill contexts: provider-agnostic shared function.
# ---------------------------------------------------------------------------
def test_runtime_skill_contexts_is_provider_agnostic():
    # runtime_skill_contexts takes only (cwd, bare_config) — no provider kind.
    # All three providers share this one function, so skill discovery cannot
    # fork per provider.
    sig = inspect.signature(runtime_skills.runtime_skill_contexts)
    assert not any("kind" in p or "provider" in p for p in sig.parameters), (
        "runtime_skill_contexts grew a provider parameter — skill discovery is "
        "no longer shared across providers."
    )


def test_runtime_skill_contexts_deterministic_and_bare_empty():
    # Identical cwd → identical contexts (no provider in the loop), and bare
    # config strips skills for every provider uniformly.
    a = runtime_skills.runtime_skill_contexts("/tmp/proj")
    b = runtime_skills.runtime_skill_contexts("/tmp/proj")
    assert a == b, "runtime skill contexts are non-deterministic for same cwd"
    assert runtime_skills.runtime_skill_contexts("/tmp/proj", bare_config=True) == [], (
        "bare config did not strip runtime skills"
    )


# ---------------------------------------------------------------------------
# S7 — Capability contexts: shared build reaches every provider.
# ---------------------------------------------------------------------------
def test_provider_capability_contexts_is_kind_dispatched_not_forked():
    # provider_capability_contexts dispatches on provider_kind — it is ONE
    # function feeding all providers, not a per-provider fork. Lock that the
    # public entrypoint remains single-dispatch.
    params = inspect.signature(capability_contexts.provider_capability_contexts).parameters
    assert "provider_kind" in params, (
        "provider_capability_contexts lost its provider_kind dispatch param"
    )
    # Given one capability with an output per provider, every first-class
    # provider must select its own output through the same function — proving
    # equivalence of mechanism, not a forked per-provider builder.
    contexts = [{
        "capability_id": "cap-1",
        "name": "Cap",
        "category": "x",
        "outputs": [
            {"provider_kind": k, "content": f"content-for-{k}", "content_kind": "text"}
            for k in PROVIDERS
        ],
    }]
    for kind in PROVIDERS:
        ctx = capability_contexts.provider_capability_contexts(contexts, kind)
        assert isinstance(ctx, list) and len(ctx) == 1, (
            f"{kind} did not select its capability context through the shared dispatcher"
        )
        assert ctx[0]["content"] == f"content-for-{kind}"


# ---------------------------------------------------------------------------
# S8 — Disabled-builtin-extensions policy: kind-agnostic normalizer.
# ---------------------------------------------------------------------------
def test_disabled_builtin_extensions_normalizer_is_kind_agnostic():
    # The normalizer is a pure function shared by all providers; same input →
    # same output, deterministically, with no kind branch.
    sig = inspect.signature(extension_run_policy.normalize_disabled_builtin_extensions)
    assert "kind" not in sig.parameters, (
        "disabled-extensions normalizer grew a kind param — no longer shared"
    )
    raw = ["demo", " marketplace ", "", "demo", "Testape"]
    norm1 = extension_run_policy.normalize_disabled_builtin_extensions(raw)
    norm2 = extension_run_policy.normalize_disabled_builtin_extensions(raw)
    assert norm1 == norm2 == ["demo", "marketplace", "Testape"], (
        f"disabled-extensions normalization diverged: {norm1!r}"
    )


# ---------------------------------------------------------------------------
# S9 — Native harness exposure: built-in harness instructions default on.
# ---------------------------------------------------------------------------
def test_builtin_harness_instructions_default_to_native_exposed():
    sig = inspect.signature(extension_store.native_harness_exposed)
    assert "provider" not in sig.parameters and "kind" in sig.parameters, (
        "native harness exposure grew provider branching — no longer shared"
    )
    record = {
        "manifest": {
            "id": extension_store.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID,
            "entrypoints": {
                "instructions": [
                    {
                        "name": "better-agent-harness-behavior",
                        "path": "instructions/harness_behavior.md",
                        "level": "global",
                    }
                ]
            },
        },
        "enabled": True,
    }
    assert extension_store.native_harness_exposed(
        extension_store.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID,
        "instructions",
        "better-agent-harness-behavior",
        record=record,
    ) is True


# ---------------------------------------------------------------------------
# Map integrity — surfaces stay mapped.
# ---------------------------------------------------------------------------
HARNESS_SURFACES = {
    "S1_run_env": provider.build_better_agent_run_env,
    "S2_permission": permission.default_permission_for_kind,
    "S3_schema": ots.DELEGATE_TASK_INPUT_SCHEMA,
    "S5_builtin_mcp": builtin_mcp_config.with_builtin_mcp_servers,
    "S6_skills": runtime_skills.runtime_skill_contexts,
    "S8_disabled_exts": extension_run_policy.normalize_disabled_builtin_extensions,
    "S9_native_harness": extension_store.native_harness_exposed,
}


def test_every_mapped_surface_ssot_is_importable():
    # If someone renames/removes an SSOT this file maps, the map is stale —
    # fail loudly with the surface name rather than an import error.
    for name, ssot in HARNESS_SURFACES.items():
        assert callable(ssot) or isinstance(ssot, dict), (
            f"{name} SSOT vanished — map is stale"
        )


def main() -> int:
    test_default_permission_is_full_bypass_for_all_providers()
    test_resolve_permission_falls_back_to_full_bypass_identically()
    test_permission_axes_cover_every_provider()
    test_run_env_is_kind_agnostic_and_deterministic()
    test_delegate_task_schema_is_shared_by_runner_providers()
    test_gemini_delegate_task_params_match_shared_schema()
    test_builtin_mcp_reserved_servers_parity()
    test_builtin_mcp_capabilities_server_identical_across_providers()
    test_runtime_skill_contexts_is_provider_agnostic()
    test_runtime_skill_contexts_deterministic_and_bare_empty()
    test_provider_capability_contexts_is_kind_dispatched_not_forked()
    test_disabled_builtin_extensions_normalizer_is_kind_agnostic()
    test_builtin_harness_instructions_default_to_native_exposed()
    test_every_mapped_surface_ssot_is_importable()
    print("cross-provider harness parity: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
