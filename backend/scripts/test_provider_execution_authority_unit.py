#!/usr/bin/env python3
"""100% unit coverage for provider_execution_authority.

Covers the secret-scrub gate, canonical-snapshot serialization, the strict
frozen credential/hydration invariants, authority matching, and the
runtime_record projection. The module is pure stdlib logic (no I/O, no
ba_home); the test-home guard is engaged only to match repo convention.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-provider-exec-authority-")
import atexit  # noqa: E402

atexit.register(TEST_HOME.release)

from provider_execution_authority import (  # noqa: E402
    ProviderExecutionCredential,
    ProviderExecutionHydration,
    _canonical_provider,
    _reject_secrets,
)

GEN = "a1b2c3d4-1111-1111-1111-111111111111"


def _cred(**over) -> ProviderExecutionCredential:
    base: dict = {
        "provider_id": "p1",
        "provider_generation": GEN,
        "provider_revision": 0,
        "status": "available",
        "api_key": "k",
    }
    base.update(over)
    return ProviderExecutionCredential(**base)


# --------------------------------------------------------------------------- #
# _reject_secrets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    ["api_key", "apikey", "API-KEY", "authorization", "credential", "password", "secret", "token", "x_api_key", "api_key_y"],
)
def test_reject_secrets_blocks_secret_keys(key):
    with pytest.raises(ValueError, match="secret-free"):
        _reject_secrets({key: "v"})


def test_reject_secrets_recurses_into_lists():
    # list items are visited, and a nested dict secret is still caught
    _reject_secrets({"items": [1, 2, {"plain": "ok"}]})
    with pytest.raises(ValueError, match="secret-free"):
        _reject_secrets({"items": [{"token": "v"}]})


def test_reject_secrets_allows_ref_and_empty_values():
    # _ref/_refs suffix is the documented escape hatch; empty values are not secrets
    _reject_secrets({"api_key_ref": "ref", "token_refs": ["a"], "secret": None, "password": "", "api_key": [], "credential": {}})


def test_reject_secrets_rejects_non_string_keys():
    with pytest.raises(ValueError, match="keys must be strings"):
        _reject_secrets({1: "a"})


def test_reject_secrets_ignores_non_containers():
    _reject_secrets("scalar")
    _reject_secrets(42)


# --------------------------------------------------------------------------- #
# _canonical_provider
# --------------------------------------------------------------------------- #
def test_canonical_provider_rejects_non_object():
    with pytest.raises(TypeError, match="must be an object"):
        _canonical_provider("nope")
    with pytest.raises(TypeError, match="must be an object"):
        _canonical_provider([1, 2])


def test_canonical_provider_rejects_non_json_values():
    with pytest.raises(ValueError, match="JSON-compatible"):
        _canonical_provider({"x": float("nan")})


def test_canonical_provider_is_sorted_compact():
    snap = _canonical_provider({"b": 1, "a": 2})
    assert snap == json.dumps({"a": 2, "b": 1}, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# ProviderExecutionCredential
# --------------------------------------------------------------------------- #
def test_credential_invalid_generation():
    with pytest.raises(ValueError, match="generation must be a canonical UUID"):
        _cred(provider_generation="not-a-uuid")


def test_credential_generation_type_error_path():
    # uuid.UUID(None) raises TypeError, caught by the same handler
    with pytest.raises(ValueError, match="generation must be a canonical UUID"):
        _cred(provider_generation=None)


def test_credential_non_canonical_uuid_form_rejected():
    # valid UUID value but non-canonical string form -> str(uuid.UUID(x)) != x
    with pytest.raises(ValueError, match="credential is invalid"):
        _cred(provider_generation=GEN.upper())


@pytest.mark.parametrize(
    "over,match",
    [
        ({"provider_id": ""}, "invalid"),
        ({"provider_id": 5}, "invalid"),
        ({"provider_revision": -1}, "invalid"),
        ({"provider_revision": "0"}, "invalid"),
        ({"status": "bogus"}, "invalid"),
        ({"api_key": None}, "invalid"),
    ],
)
def test_credential_invalid_fields(over, match):
    with pytest.raises(ValueError, match=match):
        _cred(**over)


def test_credential_authoritative_flag():
    assert _cred(status="available", api_key="k").authoritative is True
    assert _cred(status="missing", api_key="k").authoritative is False
    assert _cred(status="blocked", api_key="k").authoritative is False
    assert _cred(status="available", api_key="").authoritative is False


def test_credential_api_key_not_in_repr():
    assert "k" not in repr(_cred(api_key="secret-value"))


# --------------------------------------------------------------------------- #
# ProviderExecutionHydration
# --------------------------------------------------------------------------- #
def _provider() -> dict:
    return {"id": "p1", "generation": GEN, "revision": 0, "name": "demo"}


def test_create_without_credential_round_trips():
    h = ProviderExecutionHydration.create(_provider())
    assert h.credential is None
    assert h.provider == _provider()
    assert h.runtime_record() == _provider()


def test_create_authority_mismatch_rejected():
    with pytest.raises(ValueError, match="authority does not match"):
        ProviderExecutionHydration.create(
            _provider(),
            _cred(provider_id="other", provider_generation=GEN, provider_revision=0),
        )


def test_create_secret_free_snapshot_carried():
    with pytest.raises(ValueError, match="secret-free"):
        ProviderExecutionHydration.create({"id": "p1", "api_key": "v"})


def test_provider_property_detects_corruption():
    # bypass create() to plant a non-object snapshot (the corruption guard)
    h = ProviderExecutionHydration(_provider_json="[1, 2, 3]")
    with pytest.raises(RuntimeError, match="corrupt"):
        h.provider


def test_runtime_record_with_credential_projects_fields():
    h = ProviderExecutionHydration.create(_provider(), _cred(status="available", api_key="k"))
    rec = h.runtime_record()
    assert rec["api_key"] == "k"
    assert rec["_credential_status"] == "available"
    assert rec["_credential_authoritative"] is True
