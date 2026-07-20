from __future__ import annotations

import logging

import oskeychain
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names

logger = logging.getLogger(__name__)

CANONICAL_PROVIDER_SERVICE = "better-agent-provider-credentials-v2"
LEGACY_FLAT_ACCOUNT = "anthropic-api-key"


def _account(provider_id: str) -> str:
    return f"provider:{provider_id}"


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return value[:-1] if value.endswith("\n") else value


class ProviderCredentialStore:
    def read(self, provider_id: str) -> str | None:
        account = _account(provider_id)
        canonical = _normalize(oskeychain.native_get(CANONICAL_PROVIDER_SERVICE, account))
        if canonical:
            return canonical
        return self._migrate_legacy(provider_id, account)

    def store(self, provider_id: str, value: str) -> None:
        account = _account(provider_id)
        oskeychain.native_store(CANONICAL_PROVIDER_SERVICE, account, value)
        if _normalize(oskeychain.native_get(CANONICAL_PROVIDER_SERVICE, account)) != value:
            raise RuntimeError("canonical provider credential verification failed")
        self._cleanup_legacy(provider_id, account)

    def delete(self, provider_id: str) -> None:
        account = _account(provider_id)
        for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
            oskeychain.native_delete(service, account)
        oskeychain.native_delete(CANONICAL_PROVIDER_SERVICE, account)

    def migrate_flat(self, provider_id: str) -> str | None:
        account = _account(provider_id)
        canonical = _normalize(oskeychain.native_get(CANONICAL_PROVIDER_SERVICE, account))
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
        for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
            value = _normalize(oskeychain.native_get(service, account))
            if not value:
                continue
            oskeychain.native_store(CANONICAL_PROVIDER_SERVICE, account, value)
            verified = _normalize(oskeychain.native_get(CANONICAL_PROVIDER_SERVICE, account))
            if verified != value:
                raise RuntimeError("canonical provider credential verification failed")
            self._cleanup_legacy(provider_id, account)
            return verified
        return None

    def _cleanup_legacy(self, provider_id: str, account: str) -> None:
        for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
            try:
                oskeychain.native_delete(service, account)
            except RuntimeError:
                logger.warning(
                    "legacy provider credential cleanup failed for %s/%s",
                    service,
                    account,
                )
