"""100% unit coverage of credential_broker/presence.py.

The presence seam is the credential broker's user-presence + master-key
boundary. Two concerns:

  * ``KeyProvider`` — the AES master key ``secret_store`` decrypts with.
    Production keeps it in the macOS keychain; the unlock window caches it.
  * ``UseGate`` — whether a single ``execute`` may run. Low-risk rides the
    open unlock window; high-risk requires a fresh per-op presence proof
    (the real biometric path fails CLOSED until wired).

Selection is FAIL-CLOSED: real implementations are used unless the test seam
(``BETTER_CLAUDE_TEST_PRESENCE``, honored only under an isolated
``BETTER_CLAUDE_HOME``) is explicitly enabled.

This module owns every branch directly. The macOS keychain (``/usr/bin/
security`` + ``oskeychain.store``) and the non-darwin ``keyring`` backend are
external boundaries — monkeypatched here so the unit suite never touches the
developer's real keychain, and so the Linux keyring branch is exercisable
from macOS.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from typing import Callable

# Isolation is supplied by scripts/conftest.py (single shared session test
# home engaged before any test module imports). This file adds no second home
# of its own — a second isolate() would repoint ba_home() mid-collection and
# diverge from the home other modules already captured. All keychain / keyring
# / subprocess IO here is monkeypatched, so no real system state is touched.

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from credential_broker import presence  # noqa: E402

# Truthy stand-in for an isolated home in the faked get_env; never used as a
# real path (the factories only select on its truthiness).
_SEAM_HOME = "/tmp/ba-presence-test-home"

# A valid 32-byte master key as stored in the keychain (hex).
_HEX_KEY = "ab" * 32
_KEY_BYTES = bytes.fromhex(_HEX_KEY)


# --------------------------------------------------------------------------- #
# fakes for external boundaries
# --------------------------------------------------------------------------- #


def _return(value: str) -> Callable[..., str]:
    def _fn(*_a, **_k):
        return value

    return _fn


def _raise(exc: BaseException) -> Callable[..., object]:
    def _fn(*_a, **_k):
        raise exc

    return _fn


def _fake_get_env(values: dict[str, str]) -> Callable[..., str]:
    """Stand-in for env_compat.get_env keyed by legacy env name."""

    def _impl(legacy_name: str, default: str = "") -> str:
        return values.get(legacy_name, default)

    return _impl


def _patch_subprocess(monkeypatch, check_output: Callable) -> None:
    """Patch check_output on the real subprocess module that presence bound.

    presence's ``except (subprocess.CalledProcessError, ...)`` needs the real
    exception types, so we swap only ``check_output`` rather than the module.
    """
    monkeypatch.setattr(presence.subprocess, "check_output", check_output)


def _patch_oskeychain(monkeypatch, store: Callable) -> None:
    monkeypatch.setattr(presence.oskeychain, "store", store)


# --------------------------------------------------------------------------- #
# _kc_services
# --------------------------------------------------------------------------- #


def test_kc_services_returns_deduped_service_tuple() -> None:
    # PRIMARY ("better-agent") then LEGACY ("better-claude"), both distinct.
    assert presence._kc_services() == ("better-agent", "better-claude")


# --------------------------------------------------------------------------- #
# KeychainKeyProvider — master_key cache + lifecycle
# --------------------------------------------------------------------------- #


def test_master_key_cache_hit_returns_cached_without_io(monkeypatch) -> None:
    io: list = []
    _patch_subprocess(monkeypatch, _raise(AssertionError("subprocess on cache hit")))
    _patch_oskeychain(monkeypatch, lambda *a, **k: io.append("store"))

    provider = presence.KeychainKeyProvider()
    provider._cache = _KEY_BYTES
    assert provider.master_key() == _KEY_BYTES
    assert io == []


def test_master_key_miss_reads_absent_then_creates_and_caches(monkeypatch) -> None:
    store_calls: list[tuple] = []
    _patch_subprocess(monkeypatch, _raise(subprocess.CalledProcessError(1, ["security"])))
    _patch_oskeychain(monkeypatch, lambda *a, **k: store_calls.append(a))

    provider = presence.KeychainKeyProvider()
    key = provider.master_key()
    assert len(key) == 32
    # Created key stored once per service (primary + legacy); same hex both times.
    assert [c[0] for c in store_calls] == ["better-agent", "better-claude"]
    assert [c[2] for c in store_calls] == [key.hex(), key.hex()]
    # Second call served from cache -> no further keychain writes.
    assert provider.master_key() is key
    assert len(store_calls) == 2


def test_master_key_read_darwin_found_uses_keychain_value(monkeypatch) -> None:
    written: list = []
    _patch_subprocess(monkeypatch, _return(_HEX_KEY + "\n"))
    _patch_oskeychain(monkeypatch, lambda *a, **k: written.append(a))

    provider = presence.KeychainKeyProvider()
    assert provider.master_key() == _KEY_BYTES
    # Existing key read back -> never created -> never stored.
    assert written == []


def test_master_key_wrong_length_raises(monkeypatch) -> None:
    # "ab" is valid hex but only 1 byte, not the required 32.
    _patch_subprocess(monkeypatch, _return("ab"))
    _patch_oskeychain(monkeypatch, lambda *a, **k: None)

    provider = presence.KeychainKeyProvider()
    with pytest.raises(RuntimeError, match="wrong length"):
        provider.master_key()


# --------------------------------------------------------------------------- #
# KeychainKeyProvider._read — every branch
# --------------------------------------------------------------------------- #


def test_read_darwin_called_process_error_continues_to_none(monkeypatch) -> None:
    _patch_subprocess(monkeypatch, _raise(subprocess.CalledProcessError(1, ["security"])))
    provider = presence.KeychainKeyProvider()
    assert provider._read() is None


def test_read_darwin_timeout_continues_to_none(monkeypatch) -> None:
    _patch_subprocess(
        monkeypatch, _raise(subprocess.TimeoutExpired(["security"], presence._KC_TIMEOUT)),
    )
    provider = presence.KeychainKeyProvider()
    assert provider._read() is None


def test_read_darwin_empty_output_continues_to_next_service(monkeypatch) -> None:
    # `/usr/bin/security` exits 0 but prints only whitespace -> strip() is
    # falsy -> not returned -> loop advances to the next service -> None.
    _patch_subprocess(monkeypatch, _return("   \n"))
    provider = presence.KeychainKeyProvider()
    assert provider._read() is None


def test_read_non_darwin_keyring_hit_returns_value(monkeypatch) -> None:
    _patch_subprocess(monkeypatch, _raise(AssertionError("darwin path on linux")))
    monkeypatch.setitem(
        sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, a: _HEX_KEY),
    )
    monkeypatch.setattr(presence, "sys", types.SimpleNamespace(platform="linux"))

    provider = presence.KeychainKeyProvider()
    assert provider._read() == _HEX_KEY


def test_read_non_darwin_keyring_miss_returns_none(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, a: None))
    monkeypatch.setattr(presence, "sys", types.SimpleNamespace(platform="linux"))

    provider = presence.KeychainKeyProvider()
    assert provider._read() is None


def test_read_non_darwin_returns_first_service_with_value(monkeypatch) -> None:
    seen: list[str] = []

    def _get(service: str, account: str):
        seen.append(service)
        return _HEX_KEY if service == "better-agent" else None

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=_get))
    monkeypatch.setattr(presence, "sys", types.SimpleNamespace(platform="linux"))

    provider = presence.KeychainKeyProvider()
    assert provider._read() == _HEX_KEY
    assert seen == ["better-agent"]


# --------------------------------------------------------------------------- #
# KeychainKeyProvider._create
# --------------------------------------------------------------------------- #


def test_create_generates_32_byte_hex_and_stores_all_services(monkeypatch) -> None:
    store_calls: list[tuple] = []
    _patch_oskeychain(monkeypatch, lambda *a, **k: store_calls.append(a))

    provider = presence.KeychainKeyProvider()
    key_hex = provider._create()
    assert len(bytes.fromhex(key_hex)) == 32
    assert [c[0] for c in store_calls] == ["better-agent", "better-claude"]
    assert all(c[1] == presence._KC_ACCOUNT for c in store_calls)
    assert all(c[2] == key_hex for c in store_calls)


# --------------------------------------------------------------------------- #
# DesktopUseGate — fail-closed real gate
# --------------------------------------------------------------------------- #


def test_desktop_gate_low_risk_allowed() -> None:
    gate = presence.DesktopUseGate()
    assert gate.authorize(risk="low", consent_id="c", proof=None) is True


def test_desktop_gate_high_risk_denied_without_presence() -> None:
    gate = presence.DesktopUseGate()
    # High-risk fails CLOSED even with a proof string: the real biometric
    # verification path is not wired, so nothing dangerous may run.
    assert gate.authorize(risk="high", consent_id="c", proof="anything") is False


# --------------------------------------------------------------------------- #
# _EnvUseGate — every TEST_PRESENCE mode
# --------------------------------------------------------------------------- #


def _env_gate(monkeypatch, mode: str) -> presence._EnvUseGate:
    monkeypatch.setattr(
        presence, "get_env", _fake_get_env({"BETTER_CLAUDE_TEST_PRESENCE": mode}),
    )
    return presence._EnvUseGate()


def test_env_gate_allow_authorizes_any_risk(monkeypatch) -> None:
    gate = _env_gate(monkeypatch, "allow")
    assert gate.authorize(risk="low", consent_id="c", proof=None) is True
    assert gate.authorize(risk="high", consent_id="c", proof=None) is True


def test_env_gate_deny_rejects_any_risk(monkeypatch) -> None:
    gate = _env_gate(monkeypatch, "deny")
    assert gate.authorize(risk="low", consent_id="c", proof=None) is False
    assert gate.authorize(risk="high", consent_id="c", proof=None) is False


def test_env_gate_window_only_allows_low_denies_high(monkeypatch) -> None:
    gate = _env_gate(monkeypatch, "window-only")
    assert gate.authorize(risk="low", consent_id="c", proof=None) is True
    assert gate.authorize(risk="high", consent_id="c", proof=None) is False


def test_env_gate_unknown_mode_fails_closed(monkeypatch) -> None:
    gate = _env_gate(monkeypatch, "bogus-mode")
    assert gate.authorize(risk="low", consent_id="c", proof=None) is False
    assert gate.authorize(risk="high", consent_id="c", proof=None) is False


# --------------------------------------------------------------------------- #
# _FixedKeyProvider — deterministic test key
# --------------------------------------------------------------------------- #


def test_fixed_key_provider_returns_deterministic_32_bytes() -> None:
    provider = presence._FixedKeyProvider()
    key = provider.master_key()
    assert isinstance(key, bytes)
    assert len(key) == 32
    assert provider.master_key() == key  # stable for the same isolated home


# --------------------------------------------------------------------------- #
# _test_seam_enabled
# --------------------------------------------------------------------------- #


def test_seam_enabled_when_home_and_presence_both_set(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": "allow"}),
    )
    assert presence._test_seam_enabled() is True


def test_seam_disabled_when_home_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": "", "BETTER_CLAUDE_TEST_PRESENCE": "allow"}),
    )
    assert presence._test_seam_enabled() is False


def test_seam_disabled_when_presence_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": ""}),
    )
    assert presence._test_seam_enabled() is False


# --------------------------------------------------------------------------- #
# Factory selection — get_key_provider / get_use_gate
# --------------------------------------------------------------------------- #


def test_get_key_provider_seam_enabled_returns_fixed(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": "allow"}),
    )
    assert isinstance(presence.get_key_provider(), presence._FixedKeyProvider)


def test_get_key_provider_seam_disabled_returns_keychain(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": ""}),
    )
    assert isinstance(presence.get_key_provider(), presence.KeychainKeyProvider)


def test_get_use_gate_seam_enabled_returns_env(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": "allow"}),
    )
    assert isinstance(presence.get_use_gate(), presence._EnvUseGate)


def test_get_use_gate_seam_disabled_returns_desktop(monkeypatch) -> None:
    monkeypatch.setattr(
        presence, "get_env",
        _fake_get_env({"BETTER_CLAUDE_HOME": _SEAM_HOME, "BETTER_CLAUDE_TEST_PRESENCE": ""}),
    )
    assert isinstance(presence.get_use_gate(), presence.DesktopUseGate)
