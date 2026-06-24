"""User-presence + master-key seam.

Two responsibilities, both behind interfaces so the wiring is testable
without biometric hardware:

  * ``KeyProvider`` — supplies the AES master key used by ``secret_store``.
    Production keeps it in the keychain (and, on a signed build, behind a
    ``SecAccessControl`` user-presence ACL so a headless same-uid read
    fails). The unlock window decrypts/holds it for N minutes.

  * ``UseGate`` — decides whether a single ``execute`` may proceed. Low-risk
    ops pass on an open unlock window; high-risk ops require a fresh
    per-op presence proof (desktop Touch ID or a phone enclave signature).

Selection is FAIL-CLOSED: the real implementations are used unless the test
seam is explicitly enabled via ``BETTER_CLAUDE_TEST_PRESENCE`` (only honored
when ``BETTER_CLAUDE_HOME`` is set, i.e. an isolated test/dev home). An unset
or unknown value means real presence is required.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import Optional, Protocol

import oskeychain
from env_compat import get_env
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names
from paths import ba_home

_KC_SERVICE = PRIMARY_SERVICE
_KC_LEGACY_SERVICE = LEGACY_SERVICE
_KC_ACCOUNT = "credential-master-key"
_KC_TIMEOUT = 5


def _kc_services() -> tuple[str, ...]:
    return service_names(_KC_SERVICE, _KC_LEGACY_SERVICE)


class KeyProvider(Protocol):
    def master_key(self) -> bytes: ...


class UseGate(Protocol):
    def authorize(self, *, risk: str, consent_id: str, proof: Optional[str]) -> bool:
        ...


# ── Real implementations ─────────────────────────────────────────────────


class KeychainKeyProvider:
    """Master key persisted in the macOS keychain via /usr/bin/security
    (same caller-ACL reasoning as auth_secrets). Generated on first use."""

    def __init__(self) -> None:
        self._cache: Optional[bytes] = None

    def master_key(self) -> bytes:
        if self._cache is not None:
            return self._cache
        key_hex = self._read() or self._create()
        self._cache = bytes.fromhex(key_hex)
        if len(self._cache) != 32:
            raise RuntimeError("credential master key has wrong length")
        return self._cache

    def _read(self) -> Optional[str]:
        for service in _kc_services():
            if sys.platform != "darwin":
                import keyring

                value = keyring.get_password(service, _KC_ACCOUNT)
                if value:
                    return value
                continue
            try:
                out = subprocess.check_output(
                    ["/usr/bin/security", "find-generic-password",
                     "-s", service, "-a", _KC_ACCOUNT, "-w"],
                    stderr=subprocess.DEVNULL, text=True, timeout=_KC_TIMEOUT,
                )
                if out.strip():
                    return out.strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        return None

    def _create(self) -> str:
        key_hex = os.urandom(32).hex()
        for service in _kc_services():
            oskeychain.store(service, _KC_ACCOUNT, key_hex)
        return key_hex


class DesktopUseGate:
    """Real use gate. Low-risk → allowed (rides the unlock window). High-risk
    → requires a fresh local presence check (Touch ID via PyObjC
    LocalAuthentication) or a verified phone enclave signature.

    NOTE: the desktop biometric prompt (PyObjC ``LocalAuthentication``) and
    the phone-signature path land with the surface layer; until then this
    real gate fails-closed on high-risk ops (returns False), so nothing
    dangerous can run without an explicit presence implementation.
    """

    def authorize(self, *, risk: str, consent_id: str, proof: Optional[str]) -> bool:
        if risk == "low":
            return True
        # High-risk requires a real presence proof; not yet wired → deny.
        return False


# ── Test seam ────────────────────────────────────────────────────────────


class _FixedKeyProvider:
    """Deterministic key derived from BETTER_CLAUDE_HOME so each isolated
    test home gets its own stable key. Test-only."""

    def master_key(self) -> bytes:
        seed = str(ba_home()).encode("utf-8")
        return hashlib.sha256(b"bc-test-master-key:" + seed).digest()


class _EnvUseGate:
    """Test gate driven by BETTER_CLAUDE_TEST_PRESENCE:
      * 'allow'        — authorize everything
      * 'deny'         — authorize nothing
      * 'window-only'  — authorize low-risk only (mimics no presence proof)
    """

    def authorize(self, *, risk: str, consent_id: str, proof: Optional[str]) -> bool:
        mode = get_env("BETTER_CLAUDE_TEST_PRESENCE")
        if mode == "allow":
            return True
        if mode == "deny":
            return False
        if mode == "window-only":
            return risk == "low"
        return False


def _test_seam_enabled() -> bool:
    return bool(get_env("BETTER_CLAUDE_HOME")) and bool(get_env("BETTER_CLAUDE_TEST_PRESENCE"))


def get_key_provider() -> KeyProvider:
    if _test_seam_enabled():
        return _FixedKeyProvider()
    return KeychainKeyProvider()


def get_use_gate() -> UseGate:
    if _test_seam_enabled():
        return _EnvUseGate()
    return DesktopUseGate()
