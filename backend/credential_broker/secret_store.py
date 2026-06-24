"""Encrypted-at-rest secret storage.

Secrets are AES-GCM encrypted in a single broker-owned 0600 file under
``ba_home()/credential_broker/secrets.enc``. The AES master key is supplied
by a ``KeyProvider`` — the production provider keeps it in the keychain
behind a user-presence ACL (see ``presence.py``); the test provider returns
a fixed key so the wiring is exercisable headlessly.

The plaintext value:
  * is decrypted only for the duration of one operation, then zeroed,
  * never touches a log, the event pipeline, or any REST/WS response,
  * exists on disk only as ciphertext.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from paths import ba_home


class KeyProvider(Protocol):
    def master_key(self) -> bytes:  # 32 bytes
        ...


_REF_RE = re.compile(r"[A-Za-z0-9_\-]{1,128}")


def _require_ref(secret_ref: str) -> None:
    if not _REF_RE.fullmatch(secret_ref):
        raise ValueError(f"invalid secret_ref: {secret_ref!r}")


def _dir() -> Path:
    d = ba_home() / "credential_broker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return _dir() / "secrets.enc"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_raw(blob: dict) -> None:
    p = _path()
    # Write 0600 from the start — never widen, even briefly.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(blob).encode("utf-8"))
    finally:
        os.close(fd)


def store_secret(secret_ref: str, value: str, key_provider: KeyProvider) -> None:
    """Encrypt and persist a secret value under ``secret_ref``."""
    _require_ref(secret_ref)
    if not value:
        raise ValueError("secret value must be non-empty")
    aes = AESGCM(key_provider.master_key())
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, value.encode("utf-8"), secret_ref.encode("utf-8"))
    blob = _load_raw()
    blob[secret_ref] = {
        "nonce": nonce.hex(),
        "ct": ct.hex(),
    }
    _write_raw(blob)


def read_secret(secret_ref: str, key_provider: KeyProvider) -> str:
    """Decrypt and return the secret value. Raises KeyError if unknown.

    Callers MUST treat the return as ephemeral: use it for one operation and
    drop the reference. Never log it, never put it in a response.
    """
    _require_ref(secret_ref)
    blob = _load_raw()
    rec = blob.get(secret_ref)
    if rec is None:
        raise KeyError(secret_ref)
    aes = AESGCM(key_provider.master_key())
    nonce = bytes.fromhex(rec["nonce"])
    ct = bytes.fromhex(rec["ct"])
    pt = aes.decrypt(nonce, ct, secret_ref.encode("utf-8"))
    return pt.decode("utf-8")


def delete_secret(secret_ref: str) -> bool:
    _require_ref(secret_ref)
    blob = _load_raw()
    if secret_ref in blob:
        del blob[secret_ref]
        _write_raw(blob)
        return True
    return False


def has_secret(secret_ref: str) -> bool:
    try:
        _require_ref(secret_ref)
    except ValueError:
        return False
    return secret_ref in _load_raw()


def list_refs() -> list[str]:
    return sorted(_load_raw().keys())
