from pathlib import Path
import sys

import pytest


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from headless_keyring import Keyring


def test_headless_keyring_requires_explicit_master_key(monkeypatch) -> None:
    monkeypatch.delenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", raising=False)

    try:
        Keyring()
    except RuntimeError as exc:
        assert "BETTER_AGENT_HEADLESS_KEYRING_KEY" in str(exc)
    else:
        raise AssertionError("headless keyring accepted a missing master key")


def test_headless_keyring_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", "test-master-key")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    keyring = Keyring()

    keyring.set_password("service", "account", "secret")

    assert keyring.get_password("service", "account") == "secret"


def test_headless_keyring_rejects_relative_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", "test-master-key")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="absolute"):
        Keyring(file_path=Path("relative/keyring"))


def test_headless_keyring_uses_explicit_absolute_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", "test-master-key")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    explicit = tmp_path / "custom_keyring"
    keyring = Keyring(file_path=explicit)

    assert keyring.file_path == str(explicit)
