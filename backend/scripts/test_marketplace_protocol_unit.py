"""Unit coverage for marketplace_protocol.py — the pure validation layer
behind the Marketplace protocol (identifiers, hashes, leased-action shape).

The transport (marketplace_protocol_transport.py) already exercises the
happy path of validate_leased_action indirectly via the bridge test; this
module closes every error branch of the public validators directly.
"""
from __future__ import annotations

import pytest

import marketplace_protocol as mp


_HEX32 = "a" * 32
_ACTION_ID = "baact_" + _HEX32
_SHA256 = "a" * 64
_FUTURE = "2099-01-01T00:00:00+00:00"


def _leased(**over):
    base = {
        "action_id": _ACTION_ID,
        "action_type": "install",
        "lease_capability": "primary",
        "lease_expires_at": _FUTURE,
        "target_version": "1.0.0",
        "catalog_snapshot_sha256": _SHA256,
        "extension": {"id": "myext", "name": "My Ext", "version": "1.0.0"},
    }
    base.update(over)
    return base


# ── canonical (de)serialization ─────────────────────────────────


def test_canonical_bytes_is_sorted_compact_utf8():
    assert mp.canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_hash_is_deterministic_sha256():
    value = {"b": 1, "a": 2}
    digest = mp.canonical_hash(value)
    assert digest == mp.canonical_hash(value)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ── require_identifier ──────────────────────────────────────────


def test_require_identifier_returns_valid_value():
    assert mp.require_identifier("action", _ACTION_ID) == _ACTION_ID


@pytest.mark.parametrize("bad", ["bad", "baact_xyz", 5, None])
def test_require_identifier_rejects_invalid(bad):
    with pytest.raises(ValueError, match="invalid Marketplace action"):
        mp.require_identifier("action", bad)


# ── require_hash ────────────────────────────────────────────────


def test_require_hash_returns_valid_value():
    assert mp.require_hash(_SHA256, "fingerprint") == _SHA256


@pytest.mark.parametrize("bad", ["xyz", "A" * 64, None, 1])
def test_require_hash_rejects_invalid(bad):
    with pytest.raises(ValueError, match="fingerprint must be a lowercase sha256"):
        mp.require_hash(bad, "fingerprint")


# ── validate_leased_action: happy paths ─────────────────────────


def test_validate_leased_action_install_round_trip():
    result = mp.validate_leased_action(_leased())
    assert result["action_id"] == _ACTION_ID
    assert result["extension"] == {
        "id": "myext",
        "name": "My Ext",
        "version": "1.0.0",
        "publisher": "",
        "permission_delta": [],
    }


def test_validate_leased_action_enable_without_target_version():
    result = mp.validate_leased_action(
        _leased(action_type="enable", target_version="", extension={"id": "myext", "name": "My Ext"})
    )
    assert result["target_version"] == ""
    assert result["extension"]["version"] == ""


def test_validate_leased_action_update_with_optional_fields():
    result = mp.validate_leased_action(
        _leased(
            action_type="update",
            target_version="2.0.0",
            extension={
                "id": "myext",
                "name": "My Ext",
                "version": "2.0.0",
                "publisher": "ofek",
                "permission_delta": ["read", "write"],
            },
        )
    )
    assert result["extension"]["publisher"] == "ofek"
    assert result["extension"]["permission_delta"] == ["read", "write"]


# ── validate_leased_action: top-level shape ─────────────────────


@pytest.mark.parametrize(
    "bad",
    [None, "str", 5, {}, {"action_id": _ACTION_ID}],
)
def test_validate_leased_action_rejects_top_level_shape(bad):
    with pytest.raises(ValueError, match="leased action has an invalid shape"):
        mp.validate_leased_action(bad)


# ── validate_leased_action: extension shape ─────────────────────


def test_validate_leased_action_rejects_extension_not_dict():
    with pytest.raises(ValueError, match="extension has an invalid shape"):
        mp.validate_leased_action(_leased(extension="nope"))


def test_validate_leased_action_rejects_extension_missing_required():
    with pytest.raises(ValueError, match="extension has an invalid shape"):
        mp.validate_leased_action(_leased(extension={"id": "myext"}))


def test_validate_leased_action_rejects_extension_unknown_field():
    with pytest.raises(ValueError, match="extension has an invalid shape"):
        mp.validate_leased_action(
            _leased(extension={"id": "myext", "name": "My Ext", "version": "1.0.0", "bogus": 1})
        )


# ── validate_leased_action: identifiers / hash inside ───────────


def test_validate_leased_action_rejects_bad_action_id():
    with pytest.raises(ValueError, match="invalid Marketplace action"):
        mp.validate_leased_action(_leased(action_id="bad"))


def test_validate_leased_action_rejects_bad_catalog_snapshot_hash():
    with pytest.raises(ValueError, match="catalog_snapshot_sha256 must be a lowercase sha256"):
        mp.validate_leased_action(_leased(catalog_snapshot_sha256="xyz"))


def test_validate_leased_action_rejects_bad_extension_id():
    with pytest.raises(ValueError, match="invalid Marketplace extension"):
        mp.validate_leased_action(
            _leased(extension={"id": "BAD ID", "name": "My Ext", "version": "1.0.0"})
        )


# ── validate_leased_action: bounded text (lease_capability, name) ─


def test_validate_leased_action_rejects_non_string_capability():
    with pytest.raises(ValueError, match="lease_capability must be a string"):
        mp.validate_leased_action(_leased(lease_capability=123))


def test_validate_leased_action_rejects_blank_capability():
    with pytest.raises(ValueError, match="lease_capability is required"):
        mp.validate_leased_action(_leased(lease_capability="   "))


def test_validate_leased_action_rejects_overlong_capability():
    with pytest.raises(ValueError, match="lease_capability is invalid"):
        mp.validate_leased_action(_leased(lease_capability="x" * 501))


def test_validate_leased_action_rejects_control_char_in_capability():
    with pytest.raises(ValueError, match="lease_capability is invalid"):
        mp.validate_leased_action(_leased(lease_capability="bad\x00value"))


def test_validate_leased_action_rejects_overlong_extension_name():
    with pytest.raises(ValueError, match="extension.name is invalid"):
        mp.validate_leased_action(
            _leased(extension={"id": "myext", "name": "n" * 501, "version": "1.0.0"})
        )


# ── validate_leased_action: future timestamp ────────────────────


def test_validate_leased_action_rejects_unparseable_expiry():
    with pytest.raises(ValueError, match="lease_expires_at is invalid"):
        mp.validate_leased_action(_leased(lease_expires_at="not-a-date"))


def test_validate_leased_action_rejects_naive_expiry():
    # No timezone: treated as expired per the protocol's tz-aware rule.
    with pytest.raises(ValueError, match="lease_expires_at is expired"):
        mp.validate_leased_action(_leased(lease_expires_at="2099-01-01T00:00:00"))


def test_validate_leased_action_rejects_past_expiry():
    with pytest.raises(ValueError, match="lease_expires_at is expired"):
        mp.validate_leased_action(_leased(lease_expires_at="2000-01-01T00:00:00+00:00"))


# ── validate_leased_action: action_type ─────────────────────────


@pytest.mark.parametrize("bad", ["bogus", 5, None])
def test_validate_leased_action_rejects_invalid_action_type(bad):
    with pytest.raises(ValueError, match="leased action type is invalid"):
        mp.validate_leased_action(_leased(action_type=bad, target_version="", extension={"id": "myext", "name": "My Ext"}))


# ── validate_leased_action: permission_delta ────────────────────


def test_validate_leased_action_rejects_non_list_permission_delta():
    with pytest.raises(ValueError, match="extension.permission_delta is invalid"):
        mp.validate_leased_action(
            _leased(extension={"id": "myext", "name": "My Ext", "version": "1.0.0", "permission_delta": "read"})
        )


def test_validate_leased_action_rejects_empty_present_permission_delta():
    with pytest.raises(ValueError, match="extension.permission_delta is invalid"):
        mp.validate_leased_action(
            _leased(extension={"id": "myext", "name": "My Ext", "version": "1.0.0", "permission_delta": []})
        )


def test_validate_leased_action_rejects_too_many_permissions():
    over = {
        "id": "myext",
        "name": "My Ext",
        "version": "1.0.0",
        "permission_delta": ["p"] * (mp.PROTOCOL["bounds"]["permission_count_max"] + 1),
    }
    with pytest.raises(ValueError, match="extension.permission_delta is invalid"):
        mp.validate_leased_action(_leased(extension=over))


# ── validate_leased_action: target_version coupling ─────────────


def test_validate_leased_action_install_rejects_missing_target_version():
    with pytest.raises(ValueError, match="leased action target version is invalid"):
        mp.validate_leased_action(
            _leased(target_version="", extension={"id": "myext", "name": "My Ext"})
        )


def test_validate_leased_action_install_rejects_mismatched_target_version():
    with pytest.raises(ValueError, match="leased action target version is invalid"):
        mp.validate_leased_action(
            _leased(target_version="1.0.0", extension={"id": "myext", "name": "My Ext", "version": "2.0.0"})
        )


def test_validate_leased_action_enable_rejects_unexpected_target_version():
    with pytest.raises(ValueError, match="leased action has an unexpected target version"):
        mp.validate_leased_action(
            _leased(action_type="enable", target_version="1.0.0", extension={"id": "myext", "name": "My Ext"})
        )
