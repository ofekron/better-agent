from __future__ import annotations

import copy
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from provider_sync_authority import (  # noqa: E402
    ProviderStateConflict,
    advance_authority,
    assert_importable,
    assert_record_progress,
    new_authority,
    parse_authority,
    parse_record_authority,
    snapshot_digest,
    validate_authority,
)

_CANON_UUID = "9b5a6f36-d44c-4c3c-b54f-39554003065d"
_HEX = "0123456789abcdef"


def _digest(default_provider_id, providers) -> str:
    return snapshot_digest(default_provider_id, providers)


def _authority(default_provider_id, providers) -> dict:
    return new_authority(default_provider_id, providers)


# --- ProviderStateConflict -------------------------------------------------


def test_provider_state_conflict_copies_state_and_formats_message() -> None:
    current = {"generation": _CANON_UUID, "revision": 3, "digest": "a" * 64}
    incoming = {"generation": _CANON_UUID, "revision": 2, "digest": "b" * 64}
    exc = ProviderStateConflict("stale", current, incoming)
    assert exc.reason == "stale"
    # current/incoming are defensive copies, not the live identity.
    assert exc.current == current
    assert exc.incoming == incoming
    assert exc.current is not current
    assert exc.incoming is not incoming
    message = str(exc)
    assert "provider state conflict: stale" in message
    assert f"{_CANON_UUID}@3" in message


# --- parse_record_authority ------------------------------------------------


def test_parse_record_authority_accepts_canonical_pair() -> None:
    assert parse_record_authority(_CANON_UUID, 0) == {
        "generation": _CANON_UUID,
        "revision": 0,
    }


@pytest.mark.parametrize("bad_generation", ["not-a-uuid", 123])
def test_parse_record_authority_rejects_non_uuid_generation(
    bad_generation,
) -> None:
    with pytest.raises(ValueError, match="provider generation must be a canonical UUID"):
        parse_record_authority(bad_generation, 0)


def test_parse_record_authority_rejects_non_canonical_uuid_string() -> None:
    # Valid UUID, non-canonical (uppercase) lexical form.
    with pytest.raises(ValueError, match="provider generation must be a canonical UUID"):
        parse_record_authority(_CANON_UUID.upper(), 0)


@pytest.mark.parametrize(
    "bad_revision,fragment",
    [
        ("0", "non-negative integer"),  # not an int
        (True, "non-negative integer"),  # bool is not accepted even though int
        (-1, "non-negative integer"),  # negative
    ],
)
def test_parse_record_authority_rejects_invalid_revision(
    bad_revision, fragment
) -> None:
    with pytest.raises(ValueError, match=fragment):
        parse_record_authority(_CANON_UUID, bad_revision)


# --- parse_authority -------------------------------------------------------


def _authority_payload(**overrides) -> dict:
    base = {"generation": _CANON_UUID, "revision": 4, "digest": "c" * 64}
    base.update(overrides)
    return base


def test_parse_authority_accepts_valid_shape() -> None:
    parsed = parse_authority(_authority_payload())
    assert parsed == {
        "generation": _CANON_UUID,
        "revision": 4,
        "digest": "c" * 64,
    }


def test_parse_authority_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="must contain generation, revision, and digest"):
        parse_authority(["generation"])


@pytest.mark.parametrize(
    "authority",
    [
        {"generation": _CANON_UUID, "revision": 4},  # missing digest
        {  # extra key
            "generation": _CANON_UUID,
            "revision": 4,
            "digest": "c" * 64,
            "unexpected": True,
        },
    ],
)
def test_parse_authority_rejects_wrong_key_set(authority) -> None:
    with pytest.raises(ValueError, match="must contain generation, revision, and digest"):
        parse_authority(authority)


def test_parse_authority_rejects_non_string_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256 digest"):
        parse_authority(_authority_payload(digest=123))


def test_parse_authority_rejects_wrong_length_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256 digest"):
        parse_authority(_authority_payload(digest="c" * 10))


def test_parse_authority_rejects_non_hex_digest() -> None:
    # Correct length but contains a character outside the lowercase hex set.
    with pytest.raises(ValueError, match="SHA-256 digest"):
        parse_authority(_authority_payload(digest="G" + "c" * 63))


# --- snapshot_digest / new_authority ---------------------------------------


def test_snapshot_digest_is_deterministic_and_key_order_independent() -> None:
    # sort_keys makes dict key insertion order irrelevant; the digest is stable
    # for equal content.
    left = snapshot_digest("codex", [{"id": "codex", "kind": "codex"}])
    right = snapshot_digest("codex", [{"kind": "codex", "id": "codex"}])
    assert left == right
    assert left == snapshot_digest("codex", [{"id": "codex", "kind": "codex"}])
    assert all(char in _HEX for char in left)


def test_snapshot_digest_is_sensitive_to_provider_list_order() -> None:
    # The providers list is ordered; a reordering is a real snapshot change.
    assert snapshot_digest("codex", [{"id": "codex"}, {"id": "claude"}]) != snapshot_digest(
        "codex", [{"id": "claude"}, {"id": "codex"}]
    )


def test_snapshot_digest_changes_with_payload() -> None:
    assert snapshot_digest("codex", [{"id": "codex"}]) != snapshot_digest(
        "claude", [{"id": "codex"}]
    )


def test_new_authority_seed_shape() -> None:
    providers = [{"id": "codex"}]
    authority = new_authority("codex", providers)
    uuid.UUID(authority["generation"])  # raises if not a canonical UUID
    assert authority["revision"] == 0
    assert authority["digest"] == _digest("codex", providers)


# --- validate_authority ----------------------------------------------------


def test_validate_authority_accepts_matching_digest() -> None:
    providers = [{"id": "codex"}]
    authority = new_authority("codex", providers)
    assert validate_authority(authority, "codex", providers) == authority


def test_validate_authority_rejects_mismatched_digest() -> None:
    providers = [{"id": "codex"}]
    authority = new_authority("codex", providers)
    forged = copy.deepcopy(authority)
    forged["digest"] = "f" * 64
    with pytest.raises(ValueError, match="digest does not match its snapshot"):
        validate_authority(forged, "codex", providers)


# --- advance_authority -----------------------------------------------------


def test_advance_authority_noop_when_unchanged() -> None:
    providers = [{"id": "codex"}]
    current = new_authority("codex", providers)
    advanced = advance_authority(current, "codex", providers)
    assert advanced == current
    assert advanced is not current  # always a fresh dict


def test_advance_authority_bumps_revision_on_change() -> None:
    providers = [{"id": "codex"}]
    current = new_authority("codex", providers)
    next_providers = [{"id": "codex"}, {"id": "claude"}]
    advanced = advance_authority(current, "codex", next_providers)
    assert advanced["generation"] == current["generation"]
    assert advanced["revision"] == current["revision"] + 1
    assert advanced["digest"] == _digest("codex", next_providers)


# --- assert_importable -----------------------------------------------------


def _importable_pair() -> tuple[dict, dict]:
    providers = [{"id": "codex"}]
    current = new_authority("codex", providers)
    incoming = copy.deepcopy(current)
    return current, incoming


def test_assert_importable_accepts_equal_authority() -> None:
    current, incoming = _importable_pair()
    assert_importable(current, incoming)  # no raise


def test_assert_importable_accepts_higher_revision() -> None:
    current, incoming = _importable_pair()
    incoming["revision"] = current["revision"] + 1
    assert_importable(current, incoming)


def test_assert_importable_rejects_generation_change() -> None:
    current, incoming = _importable_pair()
    incoming["generation"] = str(uuid.uuid4())
    with pytest.raises(ProviderStateConflict, match="generation"):
        assert_importable(current, incoming)


def test_assert_importable_rejects_stale_revision() -> None:
    current, incoming = _importable_pair()
    current["revision"] = 5
    incoming["revision"] = 3
    with pytest.raises(ProviderStateConflict, match="stale"):
        assert_importable(current, incoming)


def test_assert_importable_rejects_divergent_same_revision() -> None:
    current, incoming = _importable_pair()
    incoming["digest"] = "d" * 64  # same revision, different digest
    with pytest.raises(ProviderStateConflict, match="divergent"):
        assert_importable(current, incoming)


# --- assert_record_progress ------------------------------------------------


def _record(provider_id: str, generation: str, revision: int, **extra) -> dict:
    record = {"id": provider_id, "generation": generation, "revision": revision}
    record.update(extra)
    return record


def test_assert_record_progress_accepts_higher_revision() -> None:
    gen = _CANON_UUID
    current = [_record("codex", gen, 1)]
    incoming = [_record("codex", gen, 2)]
    assert_record_progress(current, incoming, {"generation": gen, "revision": 1}, {"generation": gen, "revision": 2})


def test_assert_record_progress_skips_unknown_record() -> None:
    gen = _CANON_UUID
    current: list[dict] = []
    incoming = [_record("claude", gen, 1)]
    # Unknown id -> skipped, no conflict.
    assert_record_progress(current, incoming, {"generation": gen, "revision": 1}, {"generation": gen, "revision": 2})


def test_assert_record_progress_skips_generation_mismatch() -> None:
    gen = _CANON_UUID
    other_gen = str(uuid.uuid4())
    current = [_record("codex", gen, 1)]
    # Same id, different record generation -> skipped.
    incoming = [_record("codex", other_gen, 1)]
    assert_record_progress(current, incoming, {"generation": gen, "revision": 1}, {"generation": gen, "revision": 2})


def test_assert_record_progress_rejects_stale_record() -> None:
    gen = _CANON_UUID
    current = [_record("codex", gen, 4)]
    incoming = [_record("codex", gen, 2)]
    with pytest.raises(ProviderStateConflict, match="record_stale"):
        assert_record_progress(current, incoming, {"generation": gen, "revision": 4}, {"generation": gen, "revision": 5})


def test_assert_record_progress_rejects_divergent_record() -> None:
    gen = _CANON_UUID
    current = [_record("codex", gen, 4, nickname="old")]
    incoming = [_record("codex", gen, 4, nickname="new")]
    with pytest.raises(ProviderStateConflict, match="record_divergent"):
        assert_record_progress(current, incoming, {"generation": gen, "revision": 4}, {"generation": gen, "revision": 5})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
