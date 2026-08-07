from __future__ import annotations

import pytest

from server_config import (
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    graceful_shutdown_timeout_seconds,
)


def test_graceful_shutdown_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
        raising=False,
    )
    assert graceful_shutdown_timeout_seconds() == GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", ["0", "61", "invalid"])
def test_graceful_shutdown_timeout_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(
        "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
        value,
    )
    with pytest.raises(ValueError):
        graceful_shutdown_timeout_seconds()


@pytest.mark.parametrize("value,expected", [("1", 1), ("30", 30), ("60", 60)])
def test_graceful_shutdown_timeout_honors_valid_override(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: int,
) -> None:
    monkeypatch.setenv(
        "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
        value,
    )
    assert graceful_shutdown_timeout_seconds() == expected
