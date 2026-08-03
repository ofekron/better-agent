"""ClaudeBetterAgentRunnerProvider — the `SessionEventsProvider`-shaped
delegate `ClaudeProvider` (provider_claude.py) forwards every session-
events-shaped run-lifecycle hook to (build_env, spawn, bootstrap/tailer,
completion, recovery) when a Claude provider record configures
`runner=="better_agent_runner"` (subscription mode only — enforced by
`config_store._reject_unsupported_provider_config`).

KIND stays "claude" — this class does NOT reuse
`provider_session_events_execution.py`'s `prepare_session_events_execution`
(that shared helper's `_normalize_model`/`_STRATEGIES` kind enum has no
"claude" entry, AND — more importantly — its launch attestation always
stamps `family=strategy.kind`; reusing e.g. the "openai" strategy entry
would make `launch.family` ("openai") diverge from
`ExecutionArtifact.provider_kind` ("claude", derived from the real record),
which `execution_spawn_authority`/`provider_family_execution_runtime`'s
`family_launch_from_artifact`/`prepare_family_execution` both hard-reject
as "family launch authority mismatch" — a real, load-bearing integrity
check, not incidental). `prepare_better_agent_runner_run` (module-level
function below, called from `ClaudeProvider.prepare_run`) builds the
runner_input/launch/capabilities directly instead (mirroring
`ClaudeProvider.prepare_run`'s native branch, and reusing
`provider_family_execution_runtime.prepare_family_execution` — the SAME
low-level assembly helper the native branch already calls), consistently
keyed by `provider.KIND=="claude"` end to end. `ClaudeBetterAgentRunnerProvider`
therefore only ever receives an ALREADY-PREPARED `PreparedExecution` (via
`_start_run`) — it never calls `prepare_session_events_execution` itself,
so its KIND never needs to lie.

The underlying process is `runner_better_agent.py`, the SAME kind-agnostic
in-process agent loop openai/fugu-ba-runner drive — only the per-round
HTTP wire format differs, dispatched by `runner_better_agent.
_round_backend_for` off `inputs["provider_kind"]=="claude"` and
`inputs["provider_mode"]=="subscription"` (both stamped correctly by
`_build_better_agent_runner_input` below).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, ClassVar, Optional

from provider_session_events import SessionEventsProvider
from reasoning_effort import ALL_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT

logger = logging.getLogger(__name__)


class ClaudeBetterAgentRunnerProvider(SessionEventsProvider):
    """Drives `runner_better_agent.py` in Anthropic-Messages-API mode
    (`runner_better_agent_claude_subscription.py`) for a Claude provider
    record configured with `runner=="better_agent_runner"`. Never
    constructed or called except by `ClaudeProvider`, which forwards each
    session-events-shaped hook to an instance of this class held on
    `self._ba_delegate`. Never calls `prepare_run` on itself — `ClaudeProvider`
    builds the `PreparedExecution` directly (see this module's docstring)
    and hands it to `self._start_run`."""

    KIND: ClassVar[str] = "claude"

    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = True
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = True
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = True
    # BA's generic 5-level reasoning_effort enum — NOT Claude CLI's own
    # CLAUDE_REASONING_EFFORTS, which only applies to the native runner.
    # runner_better_agent_claude_subscription.one_round_claude_subscription
    # accepts and currently ignores reasoning_effort (documented
    # limitation there — needs a decision on mapping BA's 5-level enum
    # onto Claude's extended-thinking budget_tokens).
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ALL_REASONING_EFFORTS
    default_reasoning_effort: ClassVar[str] = DEFAULT_REASONING_EFFORT
    # No tool-less Anthropic Messages API helper exists yet — run_headless
    # below always returns None (unimplemented), never claims otherwise.
    supports_headless_no_tools: ClassVar[bool] = False

    # ------------------------------------------------------------------
    # Env — clear every foreign-provider auth var. The actual OAuth
    # bearer token is NEVER placed here: it is read fresh from the
    # macOS Keychain inside the runner subprocess, per round (see
    # runner_better_agent_claude_subscription.read_claude_subscription_token
    # via claude_subscription_credential).
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        for var in (
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
            "GEMINI_CLI_HOME",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        ):
            env.pop(var, None)
        return self.finalize_env(env)

    # ------------------------------------------------------------------
    # start_run
    # ------------------------------------------------------------------
    def _start_run(
        self,
        *,
        loop,
        queue,
        internal_token: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        _execution,
        **_unused,
    ) -> bool:
        del _unused
        self.start_session_events_execution(
            execution=_execution,
            loop=loop,
            queue=queue,
            internal_token=internal_token,
            extra_env=extra_env,
        )
        return True

    # ------------------------------------------------------------------
    # steer_run — identical shape to OpenAIProvider.steer_run (append a
    # steering message read by the next round of the in-process loop).
    # Duplicated rather than shared via a common base method to keep this
    # change scoped to Claude; a follow-up could promote this to
    # SessionEventsProvider for every in-process-runner kind.
    # ------------------------------------------------------------------
    def steer_run(
        self,
        run_id: str,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
    ) -> bool:
        rs = self._runs.get(run_id)
        images = images or []
        files = files or []
        if (
            rs is None
            or rs.popen.poll() is not None
            or (not prompt.strip() and not images and not files)
        ):
            return False
        state_path = rs.run_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not state.get("session_id"):
            return False
        inbox = rs.run_dir / "steer.jsonl"
        try:
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "prompt": prompt,
                    "images": images,
                    "files": files,
                }) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            logger.exception("claude-ba-runner steer_run failed for %s", run_id)
            return False

    # ------------------------------------------------------------------
    # run_headless — not implemented yet: there is no tool-less Anthropic
    # Messages API helper in runner_better_agent_claude_subscription.py.
    # Returns None like OpenAIProvider.run_headless does on failure;
    # callers already treat None as "headless unavailable".
    # ------------------------------------------------------------------
    async def run_headless(
        self,
        *,
        prompt: str,
        session_id: Optional[str] = None,
        resume_sid: Optional[str] = None,
        fork: bool = False,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        no_tools: bool = False,
    ) -> Optional[dict]:
        del prompt, session_id, resume_sid, fork, cwd, timeout, no_tools
        logger.warning(
            "ClaudeBetterAgentRunnerProvider.run_headless: not implemented "
            "(no tool-less Anthropic Messages API helper exists yet)",
        )
        return None


# ==============================================================================
# prepare_run for the better_agent_runner branch — module-level functions
# (not methods on ClaudeBetterAgentRunnerProvider: they build the
# PreparedExecution BEFORE any instance of that delegate is involved, and
# they operate on the real ClaudeProvider instance, keyed by
# `provider.KIND=="claude"`, per this module's docstring).
# ==============================================================================

def prepare_better_agent_runner_run(provider: Any, start_arguments: dict[str, Any]):
    """Build the `PreparedExecution` for a claude+better_agent_runner run
    directly, bypassing `prepare_session_events_execution` (see this
    module's docstring for why). Mirrors `ClaudeProvider.prepare_run`'s
    native branch section-by-section, swapping in the in-process
    runner_better_agent.py launch shape (no downstream CLI, no embedded
    SDK) and the session-events-shaped runner_input built by
    `_build_better_agent_runner_input` below.

    `provider` is the real `ClaudeProvider` instance (not this module's
    delegate) — called from `ClaudeProvider.prepare_run`.
    """
    from provider_family_launch_attestation import (
        FamilyLaunchAttestation,
        capture_cli_launch,
        capture_config_scope,
        capture_runner_launch,
    )
    from provider_family_execution_runtime import prepare_family_execution
    from provider_family_runtime_capabilities import (
        snapshot_family_runtime_capabilities,
    )
    from provider_manifest import runner_module_for
    from mcp_prewarm.preparation import prepare_runtime_mcp_prewarm
    from provider_runtime_plan_source import (
        selected_runtime_agent_sources,
        selected_runtime_skill_sources,
        structural_provider_runtime_plan,
    )
    from paths import ba_home
    from runs_dir import runs_root

    run_id = str(start_arguments.get("run_id") or "")
    if not run_id:
        raise ValueError("run id is unavailable")
    authority = provider.execution_authority_record(start_arguments)
    runner_input = _build_better_agent_runner_input(
        provider, start_arguments, authority,
    )
    run_dir = runs_root() / run_id
    # Shared, kind-agnostic session-history root runner_better_agent.py
    # itself resolves via `paths.ba_home() / "better_agent_sessions"` (see
    # runner_better_agent._sessions_root) — collision-free across kinds
    # because history files are keyed by the globally-unique
    # app_session_id, not by provider kind.
    config_root = (ba_home() / "better_agent_sessions").resolve(strict=False)
    config_root.mkdir(parents=True, exist_ok=True)
    config_root = config_root.resolve(strict=True)
    config = capture_config_scope(
        root_path=config_root,
        config_paths=(),
        resume_path=None,
    )
    runner_module = runner_module_for(provider.KIND, "better_agent_runner")
    runner_launch = capture_runner_launch(
        run_dir=run_dir,
        executable_path=sys.executable,
        runner_entry=Path(__file__).resolve().parent / f"{runner_module}.py",
        runner_kind=provider.KIND,
        runner_module=runner_module,
        frozen=bool(getattr(sys, "frozen", False)),
    )
    launch = FamilyLaunchAttestation.capture(
        family=provider.KIND,
        runner=runner_launch,
        # No separate downstream CLI — the in-process runner speaks HTTP
        # directly. Attest the same interpreter as `runner` (matching how
        # the "openai"/session-events strategy handles `cli_command is
        # None`), keyed by this provider's own kind so
        # `family_launch_from_artifact`'s `launch.family ==
        # artifact.provider_kind` check holds.
        downstream=capture_cli_launch(
            logical_command=provider.KIND,
            launcher_path=sys.executable,
            search_path=os.environ.get("PATH"),
            platform=sys.platform,
            command_processor=os.environ.get("COMSPEC"),
        ),
        config=config,
    )
    ok, attest_reason = launch.attest_with_reason()
    if not ok:
        logger.error(
            "Claude better_agent_runner launch authority attestation failed: reason=%s",
            attest_reason,
        )
        raise RuntimeError(
            "Claude better_agent_runner launch authority changed "
            f"during preparation (reason: {attest_reason})",
        )

    prewarm = prepare_runtime_mcp_prewarm(
        runner_input,
        str(start_arguments["app_session_id"]),
        bound_seconds=8.0,
    )
    runner_input["_mcp_prewarm_ready"] = prewarm.ready_map
    projection = structural_provider_runtime_plan(runner_input, provider.KIND)
    capabilities = snapshot_family_runtime_capabilities(
        family=provider.KIND,
        skill_sources=selected_runtime_skill_sources(
            runner_input["cwd"],
            bool(runner_input["bare_config"]),
            runner_input["disabled_runtime_skills"],
        ),
        agent_sources=selected_runtime_agent_sources(
            provider.KIND,
            bool(runner_input["bare_config"]),
        ),
        resolved_plan=projection["resolved_plan"],
        extension_state=projection["extension_state"],
        installation_decisions=projection["installation_decisions"],
        prewarm_results=prewarm.status,
    )
    return prepare_family_execution(
        authority,
        start_arguments=start_arguments,
        runner_input=runner_input,
        launch=launch,
        capabilities=capabilities,
    )


def _build_better_agent_runner_input(
    provider: Any,
    start_arguments: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """runner_better_agent.py's own input.json shape, kept field-for-
    field identical to what provider_session_events_execution._runner_input
    produces for the "openai" strategy (same runner module, same input
    contract) — but computed directly with `provider.KIND=="claude"`
    throughout (fallback_kind for permission, provider_kind for extension
    policy AND for the input.json field runner_better_agent.
    _round_backend_for reads to select the Anthropic Messages API
    backend) instead of borrowing "openai"'s kind for policy/permission
    purposes too.
    """
    from extension_run_policy import (
        disabled_runtime_skills_for_run,
        resolve_extension_run_policy,
    )
    from env_compat import get_env
    from permission import resolve_for_run
    from provider_session_events_execution import (
        _mode_projection,
        _normalize_mode,
        _normalize_reasoning,
        _session_records,
    )
    from provider_session_events_execution_strategy import strategy_for

    canonical_mode = _normalize_mode(provider, start_arguments)
    model = str(start_arguments.get("model") or "").strip()
    if not model:
        raise ValueError("claude provider requires a model")
    reasoning_effort = _normalize_reasoning(provider, start_arguments)
    session, worker = _session_records(start_arguments)
    policy = resolve_extension_run_policy(
        resolved_harness_run_config=start_arguments.get(
            "resolved_harness_run_config",
        ),
        session_record=session,
        worker_record=worker,
        provider_kind=provider.KIND,
        provider_run_config=start_arguments.get("provider_run_config"),
        capability_contexts=start_arguments.get("capability_contexts"),
        disabled_builtin_extensions=start_arguments.get(
            "disabled_builtin_extensions",
        ),
    )
    permission = resolve_for_run(
        sess_rec=session,
        worker_sess_rec=worker,
        is_worker=bool(start_arguments.get("is_worker")),
        fallback_kind=provider.KIND,
    )
    import extension_store
    import installation_profile
    import user_prefs

    integrations_enabled = installation_profile.integrations_enabled()
    # _mode_projection only special-cases strategy.kind=="openai" to map
    # canonical "team" -> runner-facing "manager"; the in-process runner
    # (runner_better_agent.py) needs that same projection regardless of
    # which kind drives it, so reuse the "openai" strategy purely for
    # this static field-name lookup.
    strategy = strategy_for("openai")
    # openai's strategy has no "provider_mode" entry (only "qwen"'s
    # does) — runner_better_agent._round_backend_for needs it to select
    # the Claude subscription backend, so add it explicitly rather than
    # borrowing a differently-shaped strategy.
    input_fields = strategy.input_fields | {"provider_mode"}
    values = {
        **start_arguments,
        **policy,
        **_mode_projection(strategy, canonical_mode),
        "app_session_id": str(start_arguments["app_session_id"]),
        "backend_url": (
            str(start_arguments.get("backend_url") or "").strip()
            or get_env("BETTER_CLAUDE_BACKEND_URL").strip()
            or "http://localhost:8000"
        ),
        "browser_harness_enabled": bool(
            start_arguments.get("browser_harness_enabled"),
        ),
        "context_strategy": user_prefs.get_context_strategy(),
        "files": list(start_arguments.get("files") or []),
        "images": list(start_arguments.get("images") or []),
        "internal_token": "",
        "integrations_enabled": integrations_enabled,
        "model": model,
        "permission": permission,
        "provider_id": provider.id,
        "provider_kind": provider.KIND,
        "provider_mode": str(authority.get("mode") or "subscription"),
        "reasoning_effort": reasoning_effort,
        "runner": str(authority.get("runner") or ""),
        "source": str(start_arguments.get("source") or ""),
        "team_orchestration_enabled": (
            integrations_enabled
            and extension_store.is_extension_runtime_ready(
                extension_store.extension_id_for_role("team-orchestration"),
            )
        ),
        "coordination_enabled": (
            integrations_enabled
            and extension_store.is_extension_runtime_ready(
                extension_store.BUILTIN_COORDINATION_EXTENSION_ID,
            )
        ),
        "user_facing": bool(start_arguments.get("user_facing")),
        "working_mode": session.get("working_mode"),
        "worker_working_mode": worker.get("working_mode"),
        "provisioned_tool_profile": str(
            start_arguments.get("provisioned_tool_profile") or "",
        ).strip(),
        "disabled_runtime_skills": disabled_runtime_skills_for_run(
            session_record=session,
            worker_record=worker,
        ),
    }
    values["required_mcp_server_names"] = sorted(
        extension_store.required_profile_mcp_server_names(values),
    )
    return {key: values.get(key) for key in input_fields}
