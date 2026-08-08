"""Hermetic unit coverage for provider_credentials.ProviderCredentialStore.

test_provider_credential_authority.py owns the broker/authority integration
surface (interactive retry, restart survival, denial/recovery flows against
the real oskeychain partition model) and already drives the store to ~90%.
This file closes the residual pure-logic branches hermetically via an
in-memory keychain double: the canonical adopt passthrough, every
migrate_flat arm, the legacy-miss returns, candidate validation, the
access-blocked translation, the verify-mismatch guard, and legacy-cleanup
error tolerance.
"""

from __future__ import annotations

import _test_home

_test_home.isolate("bc-test-provider-credentials-unit-")

import pytest

from keychain_names import PRIMARY_SERVICE, LEGACY_SERVICE, service_names
from provider_credentials import (
    CANONICAL_PROVIDER_SERVICE,
    LEGACY_FLAT_ACCOUNT,
    LEGACY_PROVIDER_CREDENTIAL_SERVICES,
    PROVIDER_CREDENTIAL_SERVICES,
    ProviderCredentialAccessBlocked,
    ProviderCredentialCandidate,
    ProviderCredentialStore,
)
from provider_credentials import _account, _normalize


class _FakeKeychain:
    """In-memory stand-in for oskeychain matching the store's call surface."""

    def __init__(
        self,
        entries=None,
        persist_stores=True,
        *,
        get_failures=None,
        native_get_failures=None,
        native_delete_failures=None,
    ):
        self.data: dict[tuple[str, str], str] = dict(entries or {})
        self.persist_stores = persist_stores
        self.store_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.native_delete_calls: list[tuple[str, str]] = []
        self.native_get_calls: list[tuple[str, str]] = []
        self.get_failures: set[tuple[str, str]] = set(get_failures or ())
        self.native_get_failures: set[tuple[str, str]] = set(native_get_failures or ())
        self.native_delete_failures: set[tuple[str, str]] = set(native_delete_failures or ())

    def get(self, service, account, **kwargs):
        if (service, account) in self.get_failures:
            raise RuntimeError("OS credential read was denied or unavailable")
        return self.data.get((service, account))

    def native_get(self, service, account):
        self.native_get_calls.append((service, account))
        if (service, account) in self.native_get_failures:
            raise RuntimeError("OS credential read was denied or unavailable")
        return self.data.get((service, account))

    def store(self, service, account, value):
        self.store_calls.append((service, account, value))
        if self.persist_stores:
            self.data[(service, account)] = value

    def delete(self, service, account):
        self.delete_calls.append((service, account))
        self.data.pop((service, account), None)

    def native_delete(self, service, account):
        self.native_delete_calls.append((service, account))
        if (service, account) in self.native_delete_failures:
            raise RuntimeError("OS credential delete was denied")
        self.data.pop((service, account), None)


def _legacy_candidate(provider_id: str, service: str) -> ProviderCredentialCandidate:
    return ProviderCredentialCandidate(service, _account(provider_id))


# --------------------------------------------------------------------------- #
# _normalize
# --------------------------------------------------------------------------- #


def test_normalize_collapses_empty_and_strips_trailing_newline():
    assert _normalize(None) == ""
    assert _normalize("") == ""
    assert _normalize("sk-ant\n") == "sk-ant"
    assert _normalize("sk-ant") == "sk-ant"


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


def test_read_returns_none_and_exhausts_legacy_when_keychain_empty():
    kc = _FakeKeychain()
    store = ProviderCredentialStore(keychain=kc)

    assert store.read("anthropic") is None

    # canonical was probed, then every legacy service was scanned before giving up
    expected_legacy = {(s, _account("anthropic")) for s in LEGACY_PROVIDER_CREDENTIAL_SERVICES}
    assert expected_legacy <= set(kc.native_get_calls)


def test_read_returns_canonical_without_scanning_legacy():
    account = _account("anthropic")
    kc = _FakeKeychain(entries={(CANONICAL_PROVIDER_SERVICE, account): "sk-canonical"})
    store = ProviderCredentialStore(keychain=kc)

    assert store.read("anthropic") == "sk-canonical"
    assert kc.native_get_calls == []  # legacy never touched when canonical hits


def test_read_migrates_legacy_candidate_into_canonical():
    provider_id = "anthropic"
    account = _account(provider_id)
    legacy_service = LEGACY_PROVIDER_CREDENTIAL_SERVICES[0]
    kc = _FakeKeychain(entries={(legacy_service, account): "sk-legacy"})
    store = ProviderCredentialStore(keychain=kc)

    assert store.read(provider_id) == "sk-legacy"

    # the legacy value was promoted to canonical and the legacy entry cleaned up
    assert kc.data[(CANONICAL_PROVIDER_SERVICE, account)] == "sk-legacy"
    assert (legacy_service, account) not in kc.data
    assert (legacy_service, account) in kc.native_delete_calls


# --------------------------------------------------------------------------- #
# adopt_candidate
# --------------------------------------------------------------------------- #


def test_adopt_canonical_candidate_returns_value_without_storing():
    provider_id = "anthropic"
    candidate = ProviderCredentialCandidate(
        CANONICAL_PROVIDER_SERVICE, _account(provider_id)
    )
    kc = _FakeKeychain()
    store = ProviderCredentialStore(keychain=kc)

    # canonical candidates are already in the trusted partition; no re-store
    assert store.adopt_candidate(provider_id, candidate, "sk-val") == "sk-val"
    assert kc.store_calls == []
    assert kc.data == {}


def test_adopt_legacy_candidate_stores_canonical():
    provider_id = "anthropic"
    account = _account(provider_id)
    legacy_service = LEGACY_PROVIDER_CREDENTIAL_SERVICES[1]
    candidate = _legacy_candidate(provider_id, legacy_service)
    kc = _FakeKeychain()
    store = ProviderCredentialStore(keychain=kc)

    assert store.adopt_candidate(provider_id, candidate, "sk-val") == "sk-val"
    assert kc.store_calls == [(CANONICAL_PROVIDER_SERVICE, account, "sk-val")]
    assert kc.data[(CANONICAL_PROVIDER_SERVICE, account)] == "sk-val"


# --------------------------------------------------------------------------- #
# retry_candidate + validation + access-blocked translation
# --------------------------------------------------------------------------- #


def test_retry_candidate_returns_value_for_valid_legacy_candidate():
    provider_id = "anthropic"
    legacy_service = LEGACY_PROVIDER_CREDENTIAL_SERVICES[2]
    account = _account(provider_id)
    kc = _FakeKeychain(entries={(legacy_service, account): "sk-legacy"})
    store = ProviderCredentialStore(keychain=kc)

    assert (
        store.retry_candidate(provider_id, _legacy_candidate(provider_id, legacy_service))
        == "sk-legacy"
    )


def test_validate_candidate_rejects_unknown_service_and_wrong_account():
    store = ProviderCredentialStore(keychain=_FakeKeychain())

    bogus = ProviderCredentialCandidate("not-a-real-service", _account("anthropic"))
    with pytest.raises(ValueError, match="invalid provider credential candidate"):
        store.retry_candidate("anthropic", bogus)

    # correct service, wrong account
    wrong = ProviderCredentialCandidate(CANONICAL_PROVIDER_SERVICE, "provider:someone-else")
    with pytest.raises(ValueError, match="invalid provider credential candidate"):
        store.adopt_candidate("anthropic", wrong, "sk-val")


def test_read_candidate_translates_denial_to_access_blocked():
    provider_id = "anthropic"
    account = _account(provider_id)
    candidate = ProviderCredentialCandidate(CANONICAL_PROVIDER_SERVICE, account)
    kc = _FakeKeychain(get_failures={(CANONICAL_PROVIDER_SERVICE, account)})
    store = ProviderCredentialStore(keychain=kc)

    with pytest.raises(ProviderCredentialAccessBlocked) as exc_info:
        store.retry_candidate(provider_id, candidate)
    assert exc_info.value.candidate == candidate


# --------------------------------------------------------------------------- #
# migrate_flat
# --------------------------------------------------------------------------- #


def test_migrate_flat_returns_existing_canonical_and_clears_flat():
    provider_id = "anthropic"
    account = _account(provider_id)
    kc = _FakeKeychain(
        entries={
            (CANONICAL_PROVIDER_SERVICE, account): "sk-canonical",
            (PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT): "sk-flat-legacy",
            (LEGACY_SERVICE, LEGACY_FLAT_ACCOUNT): "sk-flat-legacy",
        }
    )
    store = ProviderCredentialStore(keychain=kc)

    assert store.migrate_flat(provider_id) == "sk-canonical"
    # canonical was returned untouched; both flat accounts were removed
    assert (CANONICAL_PROVIDER_SERVICE, account) in kc.data
    assert (PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT) not in kc.data
    assert (LEGACY_SERVICE, LEGACY_FLAT_ACCOUNT) not in kc.data


def test_migrate_flat_promotes_first_non_empty_flat_account():
    provider_id = "anthropic"
    account = _account(provider_id)
    flat_services = service_names(PRIMARY_SERVICE, LEGACY_SERVICE)
    # first flat service empty -> continue; second holds the value
    kc = _FakeKeychain(
        entries={
            (flat_services[0], LEGACY_FLAT_ACCOUNT): "",
            (flat_services[1], LEGACY_FLAT_ACCOUNT): "sk-flat",
        }
    )
    store = ProviderCredentialStore(keychain=kc)

    assert store.migrate_flat(provider_id) == "sk-flat"
    # promoted value was stored canonically; both flat entries cleared
    assert kc.data[(CANONICAL_PROVIDER_SERVICE, account)] == "sk-flat"
    assert all((svc, LEGACY_FLAT_ACCOUNT) not in kc.data for svc in flat_services)


def test_migrate_flat_returns_none_when_nothing_present():
    store = ProviderCredentialStore(keychain=_FakeKeychain())
    assert store.migrate_flat("anthropic") is None


# --------------------------------------------------------------------------- #
# store / _store_canonical guards
# --------------------------------------------------------------------------- #


def test_store_canonical_raises_on_verify_mismatch_without_cleanup():
    provider_id = "anthropic"
    account = _account(provider_id)
    kc = _FakeKeychain(persist_stores=False)  # store records but never persists
    store = ProviderCredentialStore(keychain=kc)

    with pytest.raises(RuntimeError, match="canonical provider credential verification failed"):
        store.store(provider_id, "sk-val")

    # read-back disagreed, so legacy cleanup must not have run
    assert kc.native_delete_calls == []


def test_cleanup_legacy_tolerates_native_delete_failure(caplog):
    provider_id = "anthropic"
    account = _account(provider_id)
    legacy_service = LEGACY_PROVIDER_CREDENTIAL_SERVICES[0]
    # a legacy entry whose native_delete will fail during cleanup
    kc = _FakeKeychain(
        entries={(legacy_service, account): "stale"},
        native_delete_failures={(legacy_service, account)},
    )
    store = ProviderCredentialStore(keychain=kc)

    with caplog.at_level("WARNING", logger="provider_credentials"):
        returned = store.store(provider_id, "sk-val")

    # store() is a void wrapper; the canonical write still landed and the
    # failing legacy cleanup was swallowed + logged rather than propagated
    assert returned is None
    assert kc.data[(CANONICAL_PROVIDER_SERVICE, account)] == "sk-val"
    assert "legacy provider credential cleanup failed" in caplog.text


def test_store_roundtrip_then_delete_clears_canonical_and_legacy():
    provider_id = "anthropic"
    account = _account(provider_id)
    legacy_service = LEGACY_PROVIDER_CREDENTIAL_SERVICES[3]
    kc = _FakeKeychain(entries={(legacy_service, account): "stale"})
    store = ProviderCredentialStore(keychain=kc)

    store.store(provider_id, "sk-val")
    assert store.read(provider_id) == "sk-val"  # canonical wins, no legacy scan

    store.delete(provider_id)
    assert (CANONICAL_PROVIDER_SERVICE, account) in kc.delete_calls
    assert set(kc.native_delete_calls) >= {
        (svc, account) for svc in LEGACY_PROVIDER_CREDENTIAL_SERVICES
    }


# --------------------------------------------------------------------------- #
# module constants sanity (cheap guard against a silent rename)
# --------------------------------------------------------------------------- #


def test_canonical_service_is_the_only_non_legacy_one():
    assert PROVIDER_CREDENTIAL_SERVICES[0] == CANONICAL_PROVIDER_SERVICE
    assert CANONICAL_PROVIDER_SERVICE not in LEGACY_PROVIDER_CREDENTIAL_SERVICES
    assert LEGACY_FLAT_ACCOUNT == "anthropic-api-key"
