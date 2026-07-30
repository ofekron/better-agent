from __future__ import annotations

import copy
from dataclasses import dataclass

import provider_manifest



@dataclass(frozen=True)
class RuntimeProfile:
    provider_id: str
    model: str
    reasoning_effort: str
    runner: str


# Kinds whose subscription-mode credentials better_agent_runner knows how to
# speak natively (a provider-owned OAuth/session protocol) rather than a
# plain OpenAI-compatible API key. Each such kind still resolves to the
# "openai" runtime kind at spawn time (see `runtime_kind` below) — BA owns
# the same in-process agent loop either way; only the HTTP wire protocol
# inside runner_better_agent.py differs, selected at runtime from the
# provider's own mode/credentials. A sibling kind (e.g. a future Claude
# ChatGPT-equivalent) adds its own entry here without touching this one.
SUBSCRIPTION_CAPABLE_BA_RUNNER_KINDS: frozenset[str] = frozenset({"codex"})


def supported_runners(provider_record: dict | None) -> tuple[str, ...]:
    record = provider_record or {}
    kind = record.get("kind")
    choices = provider_manifest.runner_choices_for(kind)
    mode = record.get("mode")
    if kind in SUBSCRIPTION_CAPABLE_BA_RUNNER_KINDS:
        # This kind's better_agent_runner choice speaks its own subscription
        # OAuth protocol (e.g. codex's ResponsesAPI), never the generic
        # OpenAI-compatible API-key path — only offer it in subscription mode.
        if mode == "subscription":
            return choices
        return tuple(choice for choice in choices if choice != "better_agent_runner")
    if kind == "claude":
        # better_agent_runner for Claude exists only to speak the
        # subscription OAuth wire format (see
        # runner_better_agent_claude_subscription.py); there is no
        # api_key-mode backend for it, so exclude it there rather than
        # leave a dead-end runner choice.
        if mode != "subscription":
            return tuple(choice for choice in choices if choice != "better_agent_runner")
        return choices
    if mode == "api_key":
        return choices
    return tuple(choice for choice in choices if choice != "better_agent_runner")


def default_runner(provider_record: dict | None) -> str:
    record = provider_record or {}
    choices = supported_runners(record)
    configured = str(record.get("runner") or "").strip()
    if configured in choices:
        return configured
    if choices:
        return choices[0]
    raise ValueError("provider has no supported runners")


def resolve_runner(
    provider_record: dict | None,
    requested: object = None,
    *,
    strict: bool = True,
) -> str:
    record = provider_record or {}
    runner = str(requested or "").strip()
    if not runner:
        return default_runner(record)
    choices = supported_runners(record)
    if runner in choices:
        return runner
    if not strict:
        return default_runner(record)
    kind = str(record.get("kind") or "unknown")
    allowed = ", ".join(choices) or "none"
    raise ValueError(
        f"runner {runner!r} is not supported for {kind} provider in "
        f"{record.get('mode', 'subscription')} mode; available: {allowed}"
    )


def runtime_kind(provider_record: dict, runner: object = None) -> str:
    selected = str(runner or provider_record.get("runner") or "").strip()
    kind = str(provider_record.get("kind") or "claude")
    # Claude's better_agent_runner backend still speaks Claude's own wire
    # format (Anthropic Messages API over the subscription OAuth token), so
    # it must resolve to the real Claude provider class, not collapse into
    # the generic OpenAI Chat-Completions provider like every other kind's
    # better_agent_runner choice does.
    if selected == "better_agent_runner" and kind != "claude":
        return "openai"
    return kind


def family_binds_to_kind(family: str, kind: str) -> bool:
    """Whether a runtime family is a legitimate execution family for a
    configured record kind: its own family, or the family its
    better_agent_runner choice delegates to."""
    return family == kind or family == runtime_kind(
        {"kind": kind, "runner": "better_agent_runner"},
    )


def provider_record_for_runner(provider_record: dict, runner: object = None) -> dict:
    selected = resolve_runner(provider_record, runner)
    record = copy.deepcopy(provider_record)
    record["runner"] = selected
    return record


def reasoning_efforts(
    provider_record: dict,
    runner: object = None,
    *,
    model: str = "",
) -> tuple[str, ...]:
    options = provider_record.get("reasoning_effort_options") or ()
    return tuple(str(value) for value in options if str(value or "").strip())


def fit_reasoning_effort(
    provider_record: dict | None,
    effort: object = "",
    runner: object = None,
    *,
    model: str = "",
) -> str:
    """Fit an effort inherited from another session onto this provider.

    The inherited value describes the sender's provider, so anything the target
    does not expose falls back to the target's default and then to its first
    option. Providers exposing no efforts resolve to "". An empty inherited
    value already means "no effort chosen" and is preserved as-is rather than
    promoted to a concrete one.
    """
    record = provider_record or {}
    candidate = str(effort or "").strip()
    if not candidate:
        return ""
    options = reasoning_efforts(record, runner, model=model)
    if candidate in options:
        return candidate
    default_effort = str(record.get("default_reasoning_effort") or "").strip()
    if default_effort in options:
        return default_effort
    return options[0] if options else ""


def runner_profiles(provider_record: dict) -> list[dict]:
    return [
        {
            "runner": runner,
            "reasoning_efforts": list(reasoning_efforts(provider_record, runner)),
        }
        for runner in supported_runners(provider_record)
    ]
