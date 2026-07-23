#!/usr/bin/env python3
from __future__ import annotations

import atexit
import asyncio
from contextlib import contextmanager
import ctypes
import inspect
import json
import logging
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
    LEGACY_CANONICAL_PROVIDER_SERVICES,
    LEGACY_FLAT_ACCOUNT,
    LEGACY_PROVIDER_CREDENTIAL_SERVICES,
)


@contextmanager
def _keychain_doubles(
    *,
    cli_get,
    cli_store=None,
    cli_delete=None,
    native_get=None,
    native_store=None,
    native_delete=None,
):
    """Swap every oskeychain accessor the credential store routes through."""
    keychain = provider_credentials.oskeychain
    originals = {
        name: getattr(keychain, name)
        for name in ("get", "store", "delete", "native_get", "native_store", "native_delete")
    }
    keychain.get = cli_get
    keychain.store = cli_store or (lambda *_a, **_k: None)
    keychain.delete = cli_delete or (lambda *_a, **_k: None)
    keychain.native_get = native_get or (lambda *_a, **_k: None)
    keychain.native_store = native_store or (lambda *_a, **_k: None)
    keychain.native_delete = native_delete or (lambda *_a, **_k: None)
    try:
        yield
    finally:
        for name, fn in originals.items():
            setattr(keychain, name, fn)


@contextmanager
def _capture_broker_logs():
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    broker_logger = logging.getLogger("credential_session")
    handler = _Collector(level=logging.DEBUG)
    previous_level = broker_logger.level
    broker_logger.addHandler(handler)
    broker_logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        broker_logger.removeHandler(handler)
        broker_logger.setLevel(previous_level)


def test_native_authority_disables_keychain_interaction() -> None:
    calls: list[tuple[str, int]] = []
    real_platform = provider_credentials.oskeychain.sys.platform
    real_cdll = ctypes.CDLL

    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, allowed: int) -> int:
            calls.append(("allowed", allowed))
            return 0

    class FakeSecurity:
        SecKeychainSetUserInteractionAllowed = FakeFunction()

    provider_credentials.oskeychain.sys.platform = "darwin"
    ctypes.CDLL = lambda path: (
        calls.append((path, -1)) or FakeSecurity()
    )
    try:
        provider_credentials.oskeychain.disable_native_user_interaction()
        assert calls == [
            ("/System/Library/Frameworks/Security.framework/Security", -1),
            ("allowed", 0),
        ]
    finally:
        provider_credentials.oskeychain.sys.platform = real_platform
        ctypes.CDLL = real_cdll


def test_native_interaction_is_disabled_after_failure() -> None:
    allowed_values: list[int] = []
    real_platform = provider_credentials.oskeychain.sys.platform
    real_cdll = ctypes.CDLL

    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, allowed: int) -> int:
            allowed_values.append(allowed)
            return 0

    class FakeSecurity:
        SecKeychainSetUserInteractionAllowed = FakeFunction()

    provider_credentials.oskeychain.sys.platform = "darwin"
    ctypes.CDLL = lambda _path: FakeSecurity()
    try:
        try:
            with provider_credentials.oskeychain.native_user_interaction():
                raise RuntimeError("migration failed")
        except RuntimeError as exc:
            assert str(exc) == "migration failed"
        else:
            raise AssertionError("the migration failure must propagate")
        assert allowed_values == [1, 0]
    finally:
        provider_credentials.oskeychain.sys.platform = real_platform
        ctypes.CDLL = real_cdll


def test_canonical_service_is_cli_partition_v4() -> None:
    assert CANONICAL_PROVIDER_SERVICE == "better-agent-provider-credentials-v4"
    assert LEGACY_PROVIDER_CREDENTIAL_SERVICES[0] == (
        "better-agent-provider-credentials-v3"
    )
    assert LEGACY_PROVIDER_CREDENTIAL_SERVICES[1] == (
        "better-agent-provider-credentials-v2"
    )
    assert CANONICAL_PROVIDER_SERVICE not in LEGACY_PROVIDER_CREDENTIAL_SERVICES


def test_canonical_ops_use_cli_partition_accessors() -> None:
    provider_id = "provider-cli-routing"
    account = f"provider:{provider_id}"
    values: dict[tuple[str, str], str] = {}
    cli_events: list[tuple[str, str]] = []
    native_events: list[tuple[str, str]] = []

    def cli_get(service: str, requested_account: str, **_kwargs):
        cli_events.append(("get", service))
        assert service == CANONICAL_PROVIDER_SERVICE
        return values.get((service, requested_account))

    def cli_store(service: str, requested_account: str, value: str) -> None:
        cli_events.append(("store", service))
        assert service == CANONICAL_PROVIDER_SERVICE
        values[(service, requested_account)] = value

    def cli_delete(service: str, requested_account: str) -> None:
        cli_events.append(("delete", service))
        assert service == CANONICAL_PROVIDER_SERVICE
        values.pop((service, requested_account), None)

    def native_get(service: str, _requested_account: str, **_kwargs):
        native_events.append(("get", service))
        assert service != CANONICAL_PROVIDER_SERVICE
        return None

    def native_store(service: str, _requested_account: str, _value: str) -> None:
        native_events.append(("store", service))

    def native_delete(service: str, _requested_account: str) -> None:
        native_events.append(("delete", service))
        assert service != CANONICAL_PROVIDER_SERVICE

    store = provider_credentials.ProviderCredentialStore()
    with _keychain_doubles(
        cli_get=cli_get,
        cli_store=cli_store,
        cli_delete=cli_delete,
        native_get=native_get,
        native_store=native_store,
        native_delete=native_delete,
    ):
        store.store(provider_id, "cli-secret")
        assert values[(CANONICAL_PROVIDER_SERVICE, account)] == "cli-secret"
        assert store.read(provider_id) == "cli-secret"
        store.delete(provider_id)
        assert (CANONICAL_PROVIDER_SERVICE, account) not in values
    assert ("store", CANONICAL_PROVIDER_SERVICE) in cli_events
    assert ("delete", CANONICAL_PROVIDER_SERVICE) in cli_events
    assert all(event[0] != "store" for event in native_events)
    # One legacy sweep from store()'s cleanup, one from delete().
    legacy_round = [("delete", s) for s in LEGACY_PROVIDER_CREDENTIAL_SERVICES]
    assert [e for e in native_events if e[0] == "delete"] == legacy_round * 2


def test_retry_interactively_migrates_blocked_legacy() -> None:
    provider_id = "provider-interactive-migration"
    account = f"provider:{provider_id}"
    values = {(PRIMARY_SERVICE, account): "legacy-secret"}
    interactive = False
    interaction_entries = 0
    interactive_reads: list[tuple[str, str]] = []
    canonical_cli_reads: list[tuple[str, str]] = []
    canonical_cli_stores: list[tuple[str, str]] = []
    disable_calls = 0
    real_disable = credential_session.oskeychain.disable_native_user_interaction
    real_interaction = credential_session.oskeychain.native_user_interaction

    def disable() -> None:
        nonlocal disable_calls
        disable_calls += 1

    @contextmanager
    def allow_interaction():
        nonlocal interactive, interaction_entries
        interaction_entries += 1
        interactive = True
        try:
            yield
        finally:
            interactive = False

    def cli_get(service: str, requested_account: str, **_kwargs):
        canonical_cli_reads.append((service, requested_account))
        return values.get((service, requested_account))

    def cli_store(service: str, requested_account: str, value: str) -> None:
        canonical_cli_stores.append((service, requested_account))
        values[(service, requested_account)] = value

    def native_get(service: str, requested_account: str, **_kwargs):
        target = (service, requested_account)
        if interactive:
            interactive_reads.append(target)
        if service in {PRIMARY_SERVICE, LEGACY_SERVICE} and not interactive:
            raise RuntimeError("legacy ACL denied")
        return values.get((service, requested_account))

    credential_session.oskeychain.disable_native_user_interaction = disable
    credential_session.oskeychain.native_user_interaction = allow_interaction
    broker = credential_session.ProviderCredentialBroker()
    try:
        with _keychain_doubles(
            cli_get=cli_get,
            cli_store=cli_store,
            native_get=native_get,
            native_delete=lambda service, requested_account: values.pop(
                (service, requested_account), None
            ),
        ):
            request = {
                "provider_id": provider_id,
                "request_id": "9" * 32,
            }
            assert broker.handle({**request, "op": "read"}) == {"status": "blocked"}
            assert interaction_entries == 0
            assert broker.handle({**request, "op": "retry"}) == {
                "status": "available",
                "value": "legacy-secret",
            }
            assert interaction_entries == 1
            assert interactive_reads == [(PRIMARY_SERVICE, account)]
            assert (CANONICAL_PROVIDER_SERVICE, account) in canonical_cli_reads
            assert canonical_cli_stores == [(CANONICAL_PROVIDER_SERVICE, account)]
            assert disable_calls == 1
            assert not interactive
            assert values[(CANONICAL_PROVIDER_SERVICE, account)] == "legacy-secret"
            assert (PRIMARY_SERVICE, account) not in values
    finally:
        broker.clear()
        credential_session.oskeychain.disable_native_user_interaction = real_disable
        credential_session.oskeychain.native_user_interaction = real_interaction


def test_denied_retry_does_not_scan_another_credential() -> None:
    provider_id = "provider-denied-migration"
    account = f"provider:{provider_id}"
    interactive = False
    interactive_reads: list[tuple[str, str]] = []
    all_native_reads: list[tuple[str, str]] = []
    real_disable = credential_session.oskeychain.disable_native_user_interaction
    real_interaction = credential_session.oskeychain.native_user_interaction

    @contextmanager
    def allow_interaction():
        nonlocal interactive
        interactive = True
        try:
            yield
        finally:
            interactive = False

    def native_get(service: str, requested_account: str, **_kwargs):
        target = (service, requested_account)
        all_native_reads.append(target)
        if service == PRIMARY_SERVICE:
            if interactive:
                interactive_reads.append(target)
                raise RuntimeError("user denied access")
            raise RuntimeError("legacy ACL denied")
        if service == LEGACY_SERVICE:
            raise AssertionError("a denied retry must not scan another credential")
        return None

    credential_session.oskeychain.disable_native_user_interaction = lambda: None
    credential_session.oskeychain.native_user_interaction = allow_interaction
    broker = credential_session.ProviderCredentialBroker()
    try:
        with _keychain_doubles(cli_get=lambda *_a, **_k: None, native_get=native_get):
            request = {
                "provider_id": provider_id,
                "request_id": "8" * 32,
            }
            assert broker.handle({**request, "op": "read"}) == {"status": "blocked"}
            reads_before_retry = len(all_native_reads)
            assert broker.handle({**request, "op": "retry"}) == {"status": "blocked"}
            assert all_native_reads[reads_before_retry:] == [(PRIMARY_SERVICE, account)]
            assert interactive_reads == [(PRIMARY_SERVICE, account)]
    finally:
        broker.clear()
        credential_session.oskeychain.disable_native_user_interaction = real_disable
        credential_session.oskeychain.native_user_interaction = real_interaction


def test_delete_removes_every_provider_credential_entry() -> None:
    provider_id = "provider-delete-all"
    account = f"provider:{provider_id}"
    cli_deleted: list[tuple[str, str]] = []
    native_deleted: list[tuple[str, str]] = []

    with _keychain_doubles(
        cli_get=lambda *_a, **_k: None,
        cli_delete=lambda service, requested_account: cli_deleted.append(
            (service, requested_account)
        ),
        native_delete=lambda service, requested_account: native_deleted.append(
            (service, requested_account)
        ),
    ):
        provider_credentials.ProviderCredentialStore().delete(provider_id)
    assert cli_deleted == [(CANONICAL_PROVIDER_SERVICE, account)]
    assert native_deleted == [
        (service, account) for service in LEGACY_PROVIDER_CREDENTIAL_SERVICES
    ]


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

    def cli_get(service: str, requested_account: str, **_kwargs):
        events.append(("get", service))
        return canonical.get((service, requested_account))

    def cli_store(service: str, requested_account: str, value: str) -> None:
        events.append(("store", service))
        canonical[(service, requested_account)] = value

    def native_get(service: str, requested_account: str, **_kwargs):
        events.append(("legacy_get", service))
        return legacy.get((service, requested_account))

    def native_delete(service: str, requested_account: str, **_kwargs) -> None:
        events.append(("legacy_delete", service))
        legacy.pop((service, requested_account), None)

    broker = credential_session.ProviderCredentialBroker()
    session = broker.open_session()
    session.start()
    try:
        with _keychain_doubles(
            cli_get=cli_get,
            cli_store=cli_store,
            native_get=native_get,
            native_delete=native_delete,
        ):
            assert _backend_request(session, "read", "provider-legacy") == {
                "status": "available",
                "value": "legacy-secret",
            }
            legacy_services = LEGACY_PROVIDER_CREDENTIAL_SERVICES[:2]
            assert events[:6] == [
                ("get", CANONICAL_PROVIDER_SERVICE),
                ("legacy_get", legacy_services[0]),
                ("legacy_get", legacy_services[1]),
                ("legacy_get", PRIMARY_SERVICE),
                ("store", CANONICAL_PROVIDER_SERVICE),
                ("get", CANONICAL_PROVIDER_SERVICE),
            ]
            first_delete = events.index(("legacy_delete", legacy_services[0]))
            assert first_delete > 5

            session.stop()
            session = broker.open_session()
            session.start()
            before = list(events)
            assert (
                _backend_request(session, "read", "provider-legacy")["value"]
                == "legacy-secret"
            )
            assert events == before

            broker.clear()
            session.stop()
            broker = credential_session.ProviderCredentialBroker()
            session = broker.open_session()
            session.start()
            events.clear()
            assert (
                _backend_request(session, "read", "provider-legacy")["value"]
                == "legacy-secret"
            )
            assert events == [("get", CANONICAL_PROVIDER_SERVICE)]
    finally:
        session.stop()
        broker.clear()


def test_failed_canonical_verification_never_cleans_legacy() -> None:
    events: list[str] = []
    canonical_reads = 0

    def cli_get(_service: str, _requested_account: str, **_kwargs):
        nonlocal canonical_reads
        canonical_reads += 1
        return None if canonical_reads == 1 else "wrong-secret"

    with _keychain_doubles(
        cli_get=cli_get,
        cli_store=lambda *_a, **_k: events.append("store"),
        native_get=lambda *_a, **_k: "legacy-secret",
        native_delete=lambda *_a, **_k: events.append("delete"),
    ):
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


def test_canonical_denial_never_attempts_legacy_recovery() -> None:
    legacy_reads: list[str] = []

    def cli_get(*_args, **_kwargs):
        raise RuntimeError("denied")

    with _keychain_doubles(
        cli_get=cli_get,
        native_get=lambda service, *_a, **_k: legacy_reads.append(service),
    ):
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


def test_blocked_read_reprobes_and_recovers_with_logging() -> None:
    provider_id = "provider-transient-blocked"
    account = f"provider:{provider_id}"
    values: dict[tuple[str, str], str] = {}
    failing = True

    def cli_get(service: str, requested_account: str, **_kwargs):
        if failing:
            raise RuntimeError("keychain transiently locked")
        return values.get((service, requested_account))

    broker = credential_session.ProviderCredentialBroker()
    request = {"provider_id": provider_id, "request_id": "7" * 32}
    try:
        with _keychain_doubles(cli_get=cli_get), _capture_broker_logs() as records:
            assert broker.handle({**request, "op": "read"}) == {"status": "blocked"}
            warnings = [r for r in records if r.levelno == logging.WARNING]
            assert len(warnings) == 1
            assert provider_id in warnings[0].getMessage()
            assert "keychain transiently locked" in warnings[0].getMessage()

            # Still failing: the read re-probes but only logs the transition.
            assert broker.handle({**request, "op": "read"}) == {"status": "blocked"}
            warnings = [r for r in records if r.levelno == logging.WARNING]
            assert len(warnings) == 1

            values[(CANONICAL_PROVIDER_SERVICE, account)] = "fresh-secret"
            failing = False
            assert broker.handle({**request, "op": "read"}) == {
                "status": "available",
                "value": "fresh-secret",
            }
            assert broker.handle({**request, "op": "status"}) == {"status": "available"}
    finally:
        broker.clear()


def test_explicit_reentry_replaces_blocked_legacy_canonical_entry() -> None:
    provider_id = "provider-reentry"
    account = f"provider:{provider_id}"
    values: dict[tuple[str, str], str] = {}
    events: list[tuple[str, str]] = []
    blocked_legacy = LEGACY_CANONICAL_PROVIDER_SERVICES[0]

    def cli_get(service: str, requested_account: str, **_kwargs):
        events.append(("get", service))
        return values.get((service, requested_account))

    def cli_store(service: str, requested_account: str, value: str) -> None:
        events.append(("store", service))
        values[(service, requested_account)] = value

    def native_get(service: str, requested_account: str, **_kwargs):
        events.append(("get", service))
        if service == blocked_legacy:
            raise RuntimeError("legacy ACL denied")
        return values.get((service, requested_account))

    def native_delete(service: str, _requested_account: str) -> None:
        events.append(("delete", service))
        if service == blocked_legacy:
            raise RuntimeError("legacy ACL denied")

    with _keychain_doubles(
        cli_get=cli_get,
        cli_store=cli_store,
        native_get=native_get,
        native_delete=native_delete,
    ):
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
            assert ("store", blocked_legacy) not in events
            assert broker.handle({
                "op": "read",
                "provider_id": provider_id,
                "request_id": "5" * 32,
            }) == {"status": "available", "value": "replacement-secret"}
        finally:
            broker.clear()


def test_flat_credential_migrates_inside_broker_authority() -> None:
    provider_id = "provider-flat"
    account = f"provider:{provider_id}"
    canonical: dict[tuple[str, str], str] = {}
    legacy = {(PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT): "flat-secret"}

    with _keychain_doubles(
        cli_get=lambda service, requested_account, **_k: canonical.get(
            (service, requested_account)
        ),
        cli_store=lambda service, requested_account, value: canonical.__setitem__(
            (service, requested_account), value
        ),
        native_get=lambda service, requested_account, **_k: legacy.get(
            (service, requested_account)
        ),
        native_delete=lambda service, requested_account: legacy.pop(
            (service, requested_account), None
        ),
    ):
        broker = credential_session.ProviderCredentialBroker()
        try:
            response = broker.handle({
                "op": "migrate_flat",
                "provider_id": provider_id,
                "request_id": "2" * 32,
            })
            assert response == {"status": "available", "value": "flat-secret"}
            assert canonical[(CANONICAL_PROVIDER_SERVICE, account)] == "flat-secret"
            assert (PRIMARY_SERVICE, LEGACY_FLAT_ACCOUNT) not in legacy
        finally:
            broker.clear()


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
    test_native_authority_disables_keychain_interaction()
    test_native_interaction_is_disabled_after_failure()
    test_canonical_service_is_cli_partition_v4()
    test_canonical_ops_use_cli_partition_accessors()
    test_retry_interactively_migrates_blocked_legacy()
    test_denied_retry_does_not_scan_another_credential()
    test_delete_removes_every_provider_credential_entry()
    test_legacy_credential_migrates_before_cleanup_and_survives_restart()
    test_failed_canonical_verification_never_cleans_legacy()
    test_canonical_denial_never_attempts_legacy_recovery()
    test_blocked_read_reprobes_and_recovers_with_logging()
    test_explicit_reentry_replaces_blocked_legacy_canonical_entry()
    test_flat_credential_migrates_inside_broker_authority()
    test_api_key_provider_fails_before_runtime_env_without_broker()
    test_broker_death_blocks_direct_http_consumers_before_network()
    print("OK: provider credential authority")
