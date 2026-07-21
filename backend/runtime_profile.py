from __future__ import annotations

import copy
from dataclasses import dataclass

import provider_manifest


GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_OPENAI_REASONING_EFFORTS = ("minimal", "low", "medium", "high")


@dataclass(frozen=True)
class RuntimeProfile:
    provider_id: str
    model: str
    reasoning_effort: str
    runner: str


def supported_runners(provider_record: dict | None) -> tuple[str, ...]:
    record = provider_record or {}
    choices = provider_manifest.runner_choices_for(record.get("kind"))
    if record.get("mode") == "api_key":
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
    if selected == "better_agent_runner":
        return "openai"
    return str(provider_record.get("kind") or "claude")


def provider_record_for_runner(provider_record: dict, runner: object = None) -> dict:
    selected = resolve_runner(provider_record, runner)
    record = copy.deepcopy(provider_record)
    record["runner"] = selected
    if record.get("kind") == "gemini" and selected == "better_agent_runner":
        record["base_url"] = str(record.get("base_url") or GEMINI_OPENAI_BASE_URL).rstrip("/")
    return record


def reasoning_efforts(
    provider_record: dict,
    runner: object = None,
    *,
    model: str = "",
) -> tuple[str, ...]:
    selected = resolve_runner(provider_record, runner)
    if provider_record.get("kind") == "gemini" and selected == "better_agent_runner":
        efforts = list(GEMINI_OPENAI_REASONING_EFFORTS)
        normalized_model = str(model or "").lower()
        if normalized_model.startswith("gemini-2.5") and "pro" not in normalized_model:
            efforts.insert(0, "none")
        return tuple(efforts)
    options = provider_record.get("reasoning_effort_options") or ()
    return tuple(str(value) for value in options if str(value or "").strip())


def runner_profiles(provider_record: dict) -> list[dict]:
    return [
        {
            "runner": runner,
            "reasoning_efforts": list(reasoning_efforts(provider_record, runner)),
        }
        for runner in supported_runners(provider_record)
    ]
