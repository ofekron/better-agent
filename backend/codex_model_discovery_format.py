from __future__ import annotations

import json
import unicodedata


MAX_MODEL_ID_LENGTH = 512
MAX_MODELS = 10_000
_NO_PRIORITY = 2**63 - 1


class CatalogOutputError(RuntimeError):
    pass


def _normalize_model_id(raw: object) -> str:
    if type(raw) is not str:
        raise CatalogOutputError("invalid model id")
    model_id = unicodedata.normalize("NFC", raw.strip())
    if (
        not model_id
        or len(model_id) > MAX_MODEL_ID_LENGTH
        or any(
            unicodedata.category(character).startswith("C")
            for character in model_id
        )
    ):
        raise CatalogOutputError("invalid model id")
    return model_id


def parse_models(output: bytes) -> tuple[str, ...]:
    try:
        payload = json.loads(
            output.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number"),
            ),
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise CatalogOutputError("invalid catalog output") from exc
    if (
        type(payload) is not dict
        or type(payload.get("models")) is not list
        or len(payload["models"]) > MAX_MODELS
    ):
        raise CatalogOutputError("invalid catalog output")
    priorities: dict[str, int] = {}
    for item in payload["models"]:
        if type(item) is not dict:
            raise CatalogOutputError("invalid catalog model")
        visibility = item.get("visibility")
        if visibility not in {"list", "hide"}:
            raise CatalogOutputError("invalid catalog visibility")
        if visibility == "hide":
            continue
        model_id = _normalize_model_id(item.get("slug"))
        priority = item.get("priority", _NO_PRIORITY)
        if type(priority) is not int or priority < 0:
            raise CatalogOutputError("invalid catalog priority")
        priorities[model_id] = min(priority, priorities.get(model_id, priority))
    return tuple(
        sorted(
            priorities,
            key=lambda model_id: (priorities[model_id], model_id),
        ),
    )
