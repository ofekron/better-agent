from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

import config_store
import models as models_mod
import runtime_profile


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized(value: object) -> str:
    return "".join(ch.lower() for ch in _text(value) if ch.isalnum())


def _fuzzy_matches(query: str, candidates: list[object]) -> bool:
    needle = _normalized(query)
    if not needle:
        return True
    for candidate in candidates:
        haystack = _normalized(candidate)
        if not haystack:
            continue
        if needle in haystack or haystack in needle:
            return True
        if SequenceMatcher(None, needle, haystack).ratio() >= 0.62:
            return True
    return False


def _matching_values(values: list[str], query: str) -> list[str]:
    if not _text(query):
        return values
    return [value for value in values if _fuzzy_matches(query, [value])]


def available_provider_models_response(
    provider: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
) -> dict[str, Any]:
    provider_query = _text(provider)
    model_query = _text(model)
    effort_query = _text(reasoning_effort)
    runner_query = _text(runner)
    state = config_store.list_providers()
    providers: list[dict[str, Any]] = []
    for record in state.get("providers", []):
        if record.get("suspended") is True:
            continue
        if not _fuzzy_matches(
            provider_query,
            [
                record.get("id"),
                record.get("name"),
                record.get("kind"),
                record.get("nickname"),
            ],
        ):
            continue
        supported_runners = runtime_profile.supported_runners(record)
        if not _fuzzy_matches(runner_query, list(supported_runners)):
            continue
        provider_id = _text(record.get("id"))
        matched_models = _matching_values(
            models_mod.available_models(provider_id),
            model_query,
        )
        if model_query and not matched_models:
            continue
        runtime_profiles = []
        has_matched_effort = False
        for selected_runner in supported_runners:
            if not _fuzzy_matches(runner_query, [selected_runner]):
                continue
            model_profiles = []
            for selected_model in matched_models:
                model_efforts = _matching_values(
                    list(runtime_profile.reasoning_efforts(
                        record, selected_runner, model=selected_model,
                    )),
                    effort_query,
                )
                if effort_query and not model_efforts:
                    continue
                model_profiles.append({
                    "model": selected_model,
                    "reasoning_efforts": model_efforts,
                })
                has_matched_effort = has_matched_effort or bool(model_efforts)
            if not model_profiles:
                continue
            runtime_profiles.append({
                "runner": selected_runner,
                "model_profiles": model_profiles,
            })
        if effort_query and not has_matched_effort:
            continue
        profile_defaults = config_store.provider_execution_defaults(provider_id)
        providers.append({
            "provider_id": provider_id,
            "name": record.get("name", ""),
            "nickname": record.get("nickname", ""),
            "kind": record.get("kind", ""),
            "runner": profile_defaults["runner"],
            "runtime_profiles": runtime_profiles,
            "is_default": provider_id == state.get("default_provider_id"),
            "default_model": profile_defaults["default_model"],
            "default_reasoning_effort": profile_defaults["default_reasoning_effort"],
        })
    return {
        "success": True,
        "filters": {
            "provider": provider_query,
            "model": model_query,
            "reasoning_effort": effort_query,
            "runner": runner_query,
        },
        "providers": providers,
        "count": len(providers),
    }
