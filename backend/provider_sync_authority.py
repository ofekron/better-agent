from __future__ import annotations

import hashlib
import json
import uuid

_AUTHORITY_KEYS = frozenset({"generation", "revision", "digest"})


class ProviderStateConflict(RuntimeError):
    def __init__(self, reason: str, current: dict, incoming: dict):
        self.reason = reason
        self.current = dict(current)
        self.incoming = dict(incoming)
        super().__init__(
            "provider state conflict: "
            f"{reason}; current authority is "
            f"{current['generation']}@{current['revision']}"
        )


def parse_record_authority(generation: object, revision: object) -> dict:
    try:
        parsed_generation = uuid.UUID(generation)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("provider generation must be a canonical UUID") from exc
    if str(parsed_generation) != generation:
        raise ValueError("provider generation must be a canonical UUID")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise ValueError("provider revision must be a non-negative integer")
    return {
        "generation": generation,
        "revision": revision,
    }


def parse_authority(authority: object) -> dict:
    if type(authority) is not dict or set(authority) != _AUTHORITY_KEYS:
        raise ValueError(
            "provider state authority must contain generation, revision, and digest"
        )
    parsed = parse_record_authority(
        authority.get("generation"),
        authority.get("revision"),
    )
    digest = authority.get("digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("provider state digest must be a SHA-256 digest")
    return {
        **parsed,
        "digest": digest,
    }


def snapshot_digest(default_provider_id: str | None, providers: list[dict]) -> str:
    encoded = json.dumps(
        {
            "default_provider_id": default_provider_id,
            "providers": providers,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_authority(default_provider_id: str | None, providers: list[dict]) -> dict:
    return {
        "generation": str(uuid.uuid4()),
        "revision": 0,
        "digest": snapshot_digest(default_provider_id, providers),
    }


def validate_authority(
    authority: object,
    default_provider_id: str | None,
    providers: list[dict],
) -> dict:
    parsed = parse_authority(authority)
    expected_digest = snapshot_digest(default_provider_id, providers)
    if parsed["digest"] != expected_digest:
        raise ValueError("provider state digest does not match its snapshot")
    return parsed


def advance_authority(
    current: dict,
    default_provider_id: str | None,
    providers: list[dict],
) -> dict:
    digest = snapshot_digest(default_provider_id, providers)
    if digest == current["digest"]:
        return dict(current)
    return {
        "generation": current["generation"],
        "revision": current["revision"] + 1,
        "digest": digest,
    }


def assert_importable(current: dict, incoming: dict) -> None:
    if incoming["generation"] != current["generation"]:
        raise ProviderStateConflict("generation", current, incoming)
    if incoming["revision"] < current["revision"]:
        raise ProviderStateConflict("stale", current, incoming)
    if (
        incoming["revision"] == current["revision"]
        and incoming["digest"] != current["digest"]
    ):
        raise ProviderStateConflict("divergent", current, incoming)


def assert_record_progress(
    current_records: list[dict],
    incoming_records: list[dict],
    current_authority: dict,
    incoming_authority: dict,
) -> None:
    current_by_id = {
        record["id"]: record
        for record in current_records
    }
    for incoming in incoming_records:
        current = current_by_id.get(incoming["id"])
        if (
            current is None
            or incoming["generation"] != current["generation"]
        ):
            continue
        if incoming["revision"] < current["revision"]:
            raise ProviderStateConflict(
                "record_stale",
                current_authority,
                incoming_authority,
            )
        if (
            incoming["revision"] == current["revision"]
            and incoming != current
        ):
            raise ProviderStateConflict(
                "record_divergent",
                current_authority,
                incoming_authority,
            )
