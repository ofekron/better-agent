from __future__ import annotations

import logging
from dataclasses import dataclass

import oskeychain
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names

logger = logging.getLogger(__name__)

# Canonical (v4) items are created and read exclusively through the
# /usr/bin/security CLI (oskeychain.get/store/delete). Items created that
# way carry keychain partition_id "apple-tool:" — trust bound to Apple's
# stable signed CLI — so access survives rebuilds of the credential
# authority binary. Native-API creation instead pins the creating
# binary's cdhash in the partition list (a self-signed identity has no
# team id), which silently revokes access on every rebuild; that is why
# v3 and older services are legacy. Legacy candidates are still read via
# the native API so a pinned item denies cleanly in-process (prompts
# suppressed) until the interactive retry flow adopts it into v4.
CANONICAL_PROVIDER_SERVICE = "better-agent-provider-credentials-v4"
LEGACY_CANONICAL_PROVIDER_SERVICES = (
    "better-agent-provider-credentials-v3",
    "better-agent-provider-credentials-v2",
)
LEGACY_FLAT_ACCOUNT = "anthropic-api-key"
LEGACY_PROVIDER_CREDENTIAL_SERVICES = (
    *LEGACY_CANONICAL_PROVIDER_SERVICES,
    *service_names(PRIMARY_SERVICE, LEGACY_SERVICE),
)
PROVIDER_CREDENTIAL_SERVICES = (
    CANONICAL_PROVIDER_SERVICE,
    *LEGACY_PROVIDER_CREDENTIAL_SERVICES,
)


@dataclass(frozen=True)
class ProviderCredentialCandidate:
    service: str
    account: str


class ProviderCredentialAccessBlocked(RuntimeError):
    def __init__(self, candidate: ProviderCredentialCandidate) -> None:
        super().__init__("provider credential access blocked")
        self.candidate = candidate


def _account(provider_id: str) -> str:
    return f"provider:{provider_id}"


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return value[:-1] if value.endswith("\n") else value


class ProviderCredentialStore:
    def read(self, provider_id: str) -> str | None:
        account = _account(provider_id)
        canonical = self._read_candidate(
            ProviderCredentialCandidate(CANONICAL_PROVIDER_SERVICE, account)
        )
        if canonical:
            return canonical
        return self._migrate_legacy(provider_id, account)

    def retry_candidate(
        self,
        provider_id: str,
        candidate: ProviderCredentialCandidate,
    ) -> str | None:
        self._validate_candidate(provider_id, candidate)
        return self._read_candidate(candidate)

    def adopt_candidate(
        self,
        provider_id: str,
        candidate: ProviderCredentialCandidate,
        value: str,
    ) -> str:
        self._validate_candidate(provider_id, candidate)
        if candidate.service == CANONICAL_PROVIDER_SERVICE:
            return value
        return self._store_canonical(provider_id, value)

    def store(self, provider_id: str, value: str) -> None:
        self._store_canonical(provider_id, value)

    def delete(self, provider_id: str) -> None:
        account = _account(provider_id)
        oskeychain.delete(CANONICAL_PROVIDER_SERVICE, account)
        for service in LEGACY_PROVIDER_CREDENTIAL_SERVICES:
            oskeychain.native_delete(service, account)

    def migrate_flat(self, provider_id: str) -> str | None:
        account = _account(provider_id)
        canonical = _normalize(oskeychain.get(CANONICAL_PROVIDER_SERVICE, account))
        if canonical:
            self._delete_flat()
            return canonical
        for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
            value = _normalize(oskeychain.native_get(service, LEGACY_FLAT_ACCOUNT))
            if not value:
                continue
            self.store(provider_id, value)
            self._delete_flat()
            return value
        return None

    @staticmethod
    def _delete_flat() -> None:
        for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
            oskeychain.native_delete(service, LEGACY_FLAT_ACCOUNT)

    def _migrate_legacy(self, provider_id: str, account: str) -> str | None:
        for service in LEGACY_PROVIDER_CREDENTIAL_SERVICES:
            candidate = ProviderCredentialCandidate(service, account)
            value = self._read_candidate(candidate)
            if not value:
                continue
            return self.adopt_candidate(provider_id, candidate, value)
        return None

    def _store_canonical(self, provider_id: str, value: str) -> str:
        account = _account(provider_id)
        candidate = ProviderCredentialCandidate(CANONICAL_PROVIDER_SERVICE, account)
        oskeychain.store(candidate.service, candidate.account, value)
        verified = self._read_candidate(candidate)
        if verified != value:
            raise RuntimeError("canonical provider credential verification failed")
        self._cleanup_legacy(account)
        return verified

    @staticmethod
    def _validate_candidate(
        provider_id: str,
        candidate: ProviderCredentialCandidate,
    ) -> None:
        if (
            candidate.account != _account(provider_id)
            or candidate.service not in PROVIDER_CREDENTIAL_SERVICES
        ):
            raise ValueError("invalid provider credential candidate")

    @staticmethod
    def _read_candidate(candidate: ProviderCredentialCandidate) -> str:
        reader = (
            oskeychain.get
            if candidate.service == CANONICAL_PROVIDER_SERVICE
            else oskeychain.native_get
        )
        try:
            return _normalize(reader(candidate.service, candidate.account))
        except RuntimeError as exc:
            raise ProviderCredentialAccessBlocked(candidate) from exc

    def _cleanup_legacy(self, account: str) -> None:
        for service in LEGACY_PROVIDER_CREDENTIAL_SERVICES:
            try:
                oskeychain.native_delete(service, account)
            except RuntimeError:
                logger.warning(
                    "legacy provider credential cleanup failed for %s/%s",
                    service,
                    account,
                )
