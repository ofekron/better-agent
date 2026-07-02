from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

import config_store
import models as models_mod


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
) -> dict[str, Any]:
    provider_query = _text(provider)
    model_query = _text(model)
    effort_query = _text(reasoning_effort)
    state = config_store.list_providers()
    providers: list[dict[str, Any]] = []
    for record in state.get("providers", []):
        if record.get("suspended") is True:
            continue
        if not _fuzzy_matches(
            provider_query,
            [record.get("id"), record.get("name"), record.get("kind")],
        ):
            continue
        provider_id = _text(record.get("id"))
        matched_models = _matching_values(
            models_mod.available_models(provider_id),
            model_query,
        )
        if model_query and not matched_models:
            continue
        matched_efforts = _matching_values(
            list(record.get("reasoning_effort_options") or []),
            effort_query,
        )
        if effort_query and not matched_efforts:
            continue
        providers.append({
            "provider_id": provider_id,
            "name": record.get("name", ""),
            "kind": record.get("kind", ""),
            "is_default": provider_id == state.get("default_provider_id"),
            "default_model": record.get("default_model", ""),
            "default_reasoning_effort": record.get("default_reasoning_effort", ""),
            "models": matched_models,
            "reasoning_efforts": matched_efforts,
        })
    return {
        "success": True,
        "filters": {
            "provider": provider_query,
            "model": model_query,
            "reasoning_effort": effort_query,
        },
        "providers": providers,
        "count": len(providers),
    }
