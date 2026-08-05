from __future__ import annotations

import pytest

import _test_home

_TEST_HOME = _test_home.isolate("ba-app-entry-bind-host-")

from app_entry import _primary_bind_host  # noqa: E402
from env_compat import agent_env_name  # noqa: E402
import user_prefs  # noqa: E402


LEGACY_NAME = "BETTER_CLAUDE_BACKEND_BIND_HOST"
CANONICAL_NAME = agent_env_name(LEGACY_NAME)


@pytest.fixture(autouse=True)
def isolated_bind_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CANONICAL_NAME, raising=False)
    monkeypatch.delenv(LEGACY_NAME, raising=False)
    user_prefs.set_network_bind_address("127.0.0.1")


def test_primary_bind_host_uses_canonical_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CANONICAL_NAME, "0.0.0.0")

    assert _primary_bind_host() == "0.0.0.0"


def test_primary_bind_host_prefers_canonical_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CANONICAL_NAME, "0.0.0.0")
    monkeypatch.setenv(LEGACY_NAME, "127.0.0.1")

    assert _primary_bind_host() == "0.0.0.0"


def test_primary_bind_host_supports_legacy_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LEGACY_NAME, "0.0.0.0")

    assert _primary_bind_host() == "0.0.0.0"


def test_primary_bind_host_uses_persisted_default() -> None:
    user_prefs.set_network_bind_address("0.0.0.0")

    assert _primary_bind_host() == "0.0.0.0"


def test_primary_bind_host_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CANONICAL_NAME, "localhost")

    with pytest.raises(RuntimeError, match="BETTER_AGENT_BACKEND_BIND_HOST is invalid"):
        _primary_bind_host()
