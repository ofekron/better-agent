"""Canonical provider registry — the single source of truth for every
per-kind fact that used to be scattered across if/elif chains and parallel
dicts (`_resolve_class`, app_entry runner choices, INSTALLERS,
`_GEMINI_FAMILY_KINDS`, the preempt/ui-mcp gates, credential
routing).

STRING-ONLY BY DESIGN: this module imports nothing heavy — not `provider`,
not any `provider_*` subclass, not the FastAPI graph. That lets the frozen
PyInstaller entrypoint (`app_entry.py`) import it for runner dispatch
without dragging in the provider import graph (which would cycle:
`provider_claude` → `provider`) or bloating the runner child. Consumers that
need the actual class call `provider._resolve_class(kind)`, which lazily
imports `module`/`cls` from here.

Adding a provider = add ONE entry here. The consistency test
(`scripts/test_provider_manifest_consistency.py`) fails if any consumer
drifts from this table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    # Lazy-import coordinates for provider._resolve_class.
    module: str
    cls: str
    # Runner module dispatched by app_entry's frozen entrypoint. "runner" is
    # the default Claude runner; subprocess providers name their own. None
    # for virtual kinds that never spawn a runner.
    runner_module: str | None
    # Which crash-recovery replay reader run_recovery uses: "claude" (session
    # jsonl + subagent splice), "codex" (rollout + context_window), or
    # "gemini" (session_events.jsonl, Claude-shaped). NOTE: fugu is
    # codex-based but currently recovers via the "claude" reader (it writes
    # provider_kind="fugu", which historically fell to the else branch). This
    # preserves that behavior; it is a pre-existing latent bug, flagged for a
    # separate fix — do not "correct" it here without recovery test coverage.
    recovery_family: str
    # Has an installable external CLI (drives the setup wizard). openai is
    # in-process (no CLI); gemini/fugu have no verified install command yet —
    # both are explicitly False here rather than silently absent.
    installable: bool
    # Hosts the built-in `ui` MCP server (open-file-panel). codex cannot.
    hosts_ui_mcp: bool
    # Supports codex-style context-continuation preemption (turn_manager).
    context_continuation: bool
    # Credentials routed through the Claude .env path vs the OS keyring
    # (config_store). True only for the native Claude provider.
    uses_claude_env: bool
    # Env var through which this kind's per-account credential directory is
    # selected at spawn time, isolating multiple accounts of the same kind
    # (claude→CLAUDE_CONFIG_DIR, codex/fugu→CODEX_HOME). None when the kind
    # has no env-selectable credential dir (a single shared login).
    credential_config_env: str | None = None
    # Virtual kinds (claude-remote) are coordinator-side proxies: never a
    # persisted disk provider, never resolved via get_provider, no runner.
    virtual: bool = False
    runner_choices: tuple[str, ...] = ("native",)


SPECS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        kind="claude", module="provider_claude", cls="ClaudeProvider",
        runner_module="runner", recovery_family="claude",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=True,
        credential_config_env="CLAUDE_CONFIG_DIR",
    ),
    "gemini": ProviderSpec(
        kind="gemini", module="provider_gemini", cls="GeminiProvider",
        runner_module="runner_gemini", recovery_family="gemini",
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "codex": ProviderSpec(
        kind="codex", module="provider_codex", cls="CodexProvider",
        runner_module="runner_codex", recovery_family="codex",
        installable=True, hosts_ui_mcp=False,
        context_continuation=True, uses_claude_env=False,
        credential_config_env="CODEX_HOME",
    ),
    "fugu": ProviderSpec(
        kind="fugu", module="provider_fugu", cls="FuguProvider",
        runner_module="runner_codex", recovery_family="claude",
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
        credential_config_env="CODEX_HOME",
        runner_choices=("native", "better_agent_runner"),
    ),
    "openai": ProviderSpec(
        kind="openai", module="provider_openai", cls="OpenAIProvider",
        runner_module="runner_better_agent", recovery_family="gemini",
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
        runner_choices=("better_agent_runner",),
    ),
    "agy": ProviderSpec(
        kind="agy", module="provider_agy", cls="AgyProvider",
        runner_module="runner_agy", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "copilot": ProviderSpec(
        kind="copilot", module="provider_copilot", cls="CopilotProvider",
        runner_module="runner_copilot", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "pi": ProviderSpec(
        kind="pi", module="provider_pi", cls="PiProvider",
        runner_module="runner_pi", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "qwen": ProviderSpec(
        kind="qwen", module="provider_qwen", cls="QwenProvider",
        runner_module="runner_qwen", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "cursor": ProviderSpec(
        kind="cursor", module="provider_cursor", cls="CursorProvider",
        runner_module="runner_cursor", recovery_family="gemini",
        # curl|bash installer only (no verified Windows path) — like gemini,
        # usable when the CLI is already present, not wizard-installable.
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "kimi": ProviderSpec(
        kind="kimi", module="provider_kimi", cls="KimiProvider",
        runner_module="runner_kimi", recovery_family="gemini",
        # uv-based install has no verified Windows path — not wizard-installable.
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "amp": ProviderSpec(
        kind="amp", module="provider_amp", cls="AmpProvider",
        runner_module="runner_amp", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "opencode": ProviderSpec(
        kind="opencode", module="provider_opencode", cls="OpencodeProvider",
        runner_module="runner_opencode", recovery_family="gemini",
        installable=True, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
    ),
    "claude-remote": ProviderSpec(
        kind="claude-remote", module="provider_remote", cls="RemoteProviderProxy",
        runner_module=None, recovery_family="claude",
        installable=False, hosts_ui_mcp=True,
        context_continuation=False, uses_claude_env=False,
        virtual=True,
    ),
}


def spec_for(kind: str | None) -> ProviderSpec | None:
    return SPECS.get(str(kind or ""))


def all_kinds() -> list[str]:
    return list(SPECS.keys())


def runner_kinds() -> list[str]:
    """Kinds the frozen app_entry can dispatch a runner for (non-virtual)."""
    return [k for k, s in SPECS.items() if not s.virtual]


def runner_module_for(kind: str) -> str:
    """Runner module name for a kind; 'runner' (default Claude runner) when
    the spec leaves it unset."""
    s = SPECS.get(kind)
    return (s.runner_module if s and s.runner_module else "runner")


def runner_choices_for(kind: str | None) -> tuple[str, ...]:
    spec = spec_for(kind)
    return spec.runner_choices if spec else ("native",)


def default_runner_for(kind: str | None) -> str:
    choices = runner_choices_for(kind)
    return choices[0] if choices else "native"


def installable_kinds() -> list[str]:
    return sorted(k for k, s in SPECS.items() if s.installable)


def gemini_family_kinds() -> frozenset[str]:
    return frozenset(k for k, s in SPECS.items() if s.recovery_family == "gemini")
