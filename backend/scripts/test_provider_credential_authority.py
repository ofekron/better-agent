#!/usr/bin/env python3
from __future__ import annotations

import atexit
import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-provider-credential-authority-"))
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
os.environ["BETTER_CLAUDE_HOME"] = str(TEST_HOME)
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "backend"))

import credential_session  # noqa: E402
import provider  # noqa: E402
import provider_claude  # noqa: E402
import provider_openai  # noqa: E402
import provider_credentials  # noqa: E402
import models  # noqa: E402
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE  # noqa: E402
from provider_credentials import (  # noqa: E402
    CANONICAL_PROVIDER_SERVICE,
    LEGACY_CANONICAL_PROVIDER_SERVICE,
    LEGACY_FLAT_ACCOUNT,
)


def _backend_request(session, op: str, provider_id: str) -> dict:
    env = {**os.environ, **session.backend_env(), "PYTHONPATH": str(ROOT / "backend")}
    code = (
        "import json, credential_session_client as client; "
        f"print(json.dumps(client.request({op!r}, {provider_id!r})))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        **session.backend_popen_kwargs(),
    )
    return json.loads(proc.stdout)


def test_legacy_credential_migrates_before_cleanup_and_survives_restart() -> None:
    account = "provider:provider-legacy"
    canonical: dict[tuple[str, str], str] = {}
    legacy = {(PRIMARY_SERVICE, account): "legacy-secret"}
    events: list[tuple[str, str]] = []
    real_get = provider_credentials.oskeychain.native_get
    real_store = provider_credentials.oskeychain.native_store
    real_delete = provider_credentials.oskeychain.native_delete

    def get(service: str, requested_account: str, **_kwargs):
        events.append(("get", service))
        if service == CANONICAL_PROVIDER_SERVICE:
            return canonical.get((service, requested_account))
        events[-1] = ("legacy_get", service)
        return legacy.get((service, requested_account))

    def store(service: str, requested_account: str, value: str) -> None:
        events.append(("store", service))
        canonical[(service, requested_account)] = value

    def delete_legacy(service: str, requested_account: str, **_kwargs) -> None:
        events.append(("legacy_delete", service))
        legacy.pop((service, requested_account), None)

    provider_credentials.oskeychain.native_get = get
    provider_credentials.oskeychain.native_store = store
    provider_credentials.oskeychain.native_delete = delete_legacy
    broker = credential_session.ProviderCredentialBroker()
    session = broker.open_session()
    session.start()
    try:
        assert _backend_request(session, "read", "provider-legacy") == {
            "status": "available",
            "value": "legacy-secret",
        }
        assert events[:5] == [
            ("get", CANONICAL_PROVIDER_SERVICE),
            ("legacy_get", LEGACY_CANONICAL_PROVIDER_SERVICE),
            ("legacy_get", PRIMARY_SERVICE),
            ("store", CANONICAL_PROVIDER_SERVICE),
            ("get", CANONICAL_PROVIDER_SERVICE),
        ]
        first_delete = events.index(("legacy_delete", PRIMARY_SERVICE))
        assert first_delete > 4

        session.stop()
        session = broker.open_session()
        session.start()
        before = list(events)
        assert _backend_request(session, "read", "provider-legacy")["value"] == "legacy-secret"
        assert events == before

        broker.clear()
        session.stop()
        broker = credential_session.ProviderCredentialBroker()
        session = broker.open_session()
        session.start()
        events.clear()
        assert _backend_request(session, "read", "provider-legacy")["value"] == "legacy-secret"
        assert events == [("get", CANONICAL_PROVIDER_SERVICE)]
    finally:
        session.stop()
        broker.clear()
        provider_credentials.oskeychain.native_get = real_get
        provider_credentials.oskeychain.native_store = real_store
        provider_credentials.oskeychain.native_delete = real_delete


def test_failed_canonical_verification_never_cleans_legacy() -> None:
    events: list[str] = []
    reads = 0
    real_get = provider_credentials.oskeychain.native_get
    real_store = provider_credentials.oskeychain.native_store
    real_delete = provider_credentials.oskeychain.native_delete

    def get(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return None if reads == 1 else "wrong-secret"

    provider_credentials.oskeychain.native_get = lambda service, *_args, **_kwargs: (
        get() if service == CANONICAL_PROVIDER_SERVICE else "legacy-secret"
    )
    provider_credentials.oskeychain.native_store = lambda *_args, **_kwargs: events.append("store")
    provider_credentials.oskeychain.native_delete = (
        lambda *_args, **_kwargs: events.append("delete")
    )
    broker = credential_session.ProviderCredentialBroker()
    try:
        response = broker.handle({
            "op": "read",
            "provider_id": "provider-verify-failure",
            "request_id": "0" * 32,
        })
        assert response == {"status": "blocked"}
        assert events == ["store"]
    finally:
        broker.clear()
        provider_credentials.oskeychain.native_get = real_get
        provider_credentials.oskeychain.native_store = real_store
        provider_credentials.oskeychain.native_delete = real_delete


def test_canonical_denial_never_attempts_legacy_recovery() -> None:
    real_get = provider_credentials.oskeychain.native_get
    legacy_reads: list[str] = []
    provider_credentials.oskeychain.native_get = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("denied"))
    )
    broker = credential_session.ProviderCredentialBroker()
    try:
        response = broker.handle({
            "op": "read",
            "provider_id": "provider-canonical-denied",
            "request_id": "1" * 32,
        })
        assert response == {"status": "blocked"}
        assert legacy_reads == []
    finally:
        broker.clear()
        provider_credentials.oskeychain.native_get = real_get


def test_explicit_reentry_replaces_blocked_legacy_canonical_entry() -> None:
    provider_id = "provider-reentry"
    account = f"provider:{provider_id}"
    values: dict[tuple[str, str], str] = {}
    events: list[tuple[str, str]] = []
    real_get = provider_credentials.oskeychain.native_get
    real_store = provider_credentials.oskeychain.native_store
    real_delete = provider_credentials.oskeychain.native_delete

    def get(service: str, requested_account: str):
        events.append(("get", service))
        if service == LEGACY_CANONICAL_PROVIDER_SERVICE:
            raise RuntimeError("legacy ACL denied")
        return values.get((service, requested_account))

    def store(service: str, requested_account: str, value: str) -> None:
        events.append(("store", service))
        values[(service, requested_account)] = value

    def delete(service: str, _requested_account: str) -> None:
        events.append(("delete", service))
        if service == LEGACY_CANONICAL_PROVIDER_SERVICE:
            raise RuntimeError("legacy ACL denied")

    provider_credentials.oskeychain.native_get = get
    provider_credentials.oskeychain.native_store = store
    provider_credentials.oskeychain.native_delete = delete
    broker = credential_session.ProviderCredentialBroker()
    try:
        assert broker.handle({
            "op": "read",
            "provider_id": provider_id,
            "request_id": "3" * 32,
        }) == {"status": "blocked"}
        assert broker.handle({
            "op": "store",
            "provider_id": provider_id,
            "request_id": "4" * 32,
            "value": "replacement-secret",
        }) == {"status": "available"}
        assert values[(CANONICAL_PROVIDER_SERVICE, account)] == "replacement-secret"
        assert ("store", LEGACY_CANONICAL_PROVIDER_SERVICE) not in events
        assert broker.handle({
            "op": "read",
            "provider_id": provider_id,
            "request_id": "5" * 32,
        }) == {"status": "available", "value": "replacement-secret"}
    finally:
        broker.clear()
        provider_credentials.oskeychain.native_get = real_get
        provider_credentials.oskeychain.native_store = real_store
        provider_credentials.oskeychain.native_delete = real_delete


def test_flat_credential_migrates_inside_broker_authority() -> None:
    provider_id = "provider-flat"
    account = f"provider:{provider_id}"
    values = {(PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT): "flat-secret"}
    real_get = provider_credentials.oskeychain.native_get
    real_store = provider_credentials.oskeychain.native_store
    real_delete = provider_credentials.oskeychain.native_delete
    provider_credentials.oskeychain.native_get = (
        lambda service, requested_account: values.get((service, requested_account))
    )
    provider_credentials.oskeychain.native_store = (
        lambda service, requested_account, value: values.__setitem__(
            (service, requested_account), value
        )
    )
    provider_credentials.oskeychain.native_delete = (
        lambda service, requested_account: values.pop((service, requested_account), None)
    )
    broker = credential_session.ProviderCredentialBroker()
    try:
        response = broker.handle({
            "op": "migrate_flat",
            "provider_id": provider_id,
            "request_id": "2" * 32,
        })
        assert response == {"status": "available", "value": "flat-secret"}
        assert values[(CANONICAL_PROVIDER_SERVICE, account)] == "flat-secret"
        assert (PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT) not in values
    finally:
        broker.clear()
        provider_credentials.oskeychain.native_get = real_get
        provider_credentials.oskeychain.native_store = real_store
        provider_credentials.oskeychain.native_delete = real_delete


def test_api_key_provider_fails_before_runtime_env_without_broker() -> None:
    record = {
        "id": "provider-zai",
        "kind": "claude",
        "mode": "api_key",
        "base_url": "https://api.z.ai/api/anthropic",
        "config_dir": str(TEST_HOME / ".claude-zai"),
        "api_key": "",
    }
    instance = provider_claude.ClaudeProvider(record)
    try:
        instance.build_env()
    except provider.ProviderCredentialError as exc:
        assert "provider-zai" in str(exc)
    else:
        raise AssertionError("API-key provider must fail before runtime env construction")

    record["api_key"] = "raw-secret"
    instance = provider_claude.ClaudeProvider(record)
    try:
        instance.build_env()
    except provider.ProviderCredentialError as exc:
        assert "not supervisor-authoritative" in str(exc)
    else:
        raise AssertionError("raw unmarked credentials must not bypass authority")

    record["api_key"] = "cached-secret"
    record["_credential_authoritative"] = True
    instance = provider_claude.ClaudeProvider(record)
    try:
        instance.build_env()
    except provider.ProviderCredentialError as exc:
        assert "credential is blocked" in str(exc)
    else:
        raise AssertionError("broker loss must invalidate a cached credential")

    real_status = provider.config_store.provider_credential_status
    provider.config_store.provider_credential_status = lambda _provider_id: (
        (_ for _ in ()).throw(BrokenPipeError("broker closed"))
    )
    try:
        instance.build_env()
    except provider.ProviderCredentialError as exc:
        assert "authority is unavailable" in str(exc)
    else:
        raise AssertionError("broker death must fail before provider spawn")
    finally:
        provider.config_store.provider_credential_status = real_status

    source = inspect.getsource(provider_claude.ClaudeProvider._spawn_run)
    assert source.index("self.build_env()") < source.index("subprocess.Popen(")


def test_broker_death_blocks_direct_http_consumers_before_network() -> None:
    record = {
        "id": "provider-openai",
        "kind": "openai",
        "mode": "api_key",
        "base_url": "https://example.invalid/v1",
        "default_model": "model",
        "api_key": "cached-secret",
        "_credential_authoritative": True,
    }
    instance = provider_openai.OpenAIProvider(record)
    real_status = provider.config_store.provider_credential_status
    real_completion = provider_openai._openai_headless_completion
    real_get_provider = provider.get_provider
    real_get_with_key = models.get_provider_with_key
    real_suspended = provider.config_store.provider_suspended
    network_calls: list[str] = []
    provider.config_store.provider_credential_status = lambda _provider_id: (
        (_ for _ in ()).throw(BrokenPipeError("broker closed"))
    )
    provider_openai._openai_headless_completion = (
        lambda **_kwargs: network_calls.append("headless")
    )
    provider.get_provider = lambda _provider_id: instance
    models.get_provider_with_key = lambda _provider_id: dict(record)
    provider.config_store.provider_suspended = lambda _provider_id: False
    try:
        try:
            asyncio.run(instance.run_headless(prompt="test"))
        except provider.ProviderCredentialError:
            pass
        else:
            raise AssertionError("headless HTTP must fail before network")
        assert asyncio.run(models.refresh_one(record["id"])) is None
        assert network_calls == []
    finally:
        provider.config_store.provider_credential_status = real_status
        provider_openai._openai_headless_completion = real_completion
        provider.get_provider = real_get_provider
        models.get_provider_with_key = real_get_with_key
        provider.config_store.provider_suspended = real_suspended


if __name__ == "__main__":
    test_legacy_credential_migrates_before_cleanup_and_survives_restart()
    test_failed_canonical_verification_never_cleans_legacy()
    test_canonical_denial_never_attempts_legacy_recovery()
    test_explicit_reentry_replaces_blocked_legacy_canonical_entry()
    test_flat_credential_migrates_inside_broker_authority()
    test_api_key_provider_fails_before_runtime_env_without_broker()
    test_broker_death_blocks_direct_http_consumers_before_network()
    print("OK: provider credential authority")
