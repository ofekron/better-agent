"""Unit tests for ``credential_broker.secret_store`` — encrypted-at-rest
secret storage (AES-GCM in a single 0600 file under ba_home()).

Isolation: ``secret_store`` resolves ``ba_home()`` at every call via its
module-global reference, so each test monkeypatches ``secret_store.ba_home``
to a fresh tmp dir. No real home is ever touched.
"""

from __future__ import annotations

import json
import stat

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from credential_broker import secret_store


class _Key:
    """Minimal KeyProvider returning a fixed 32-byte key."""

    def __init__(self, key: bytes) -> None:
        assert len(key) == 32
        self._key = key

    def master_key(self) -> bytes:
        return self._key


KEY_A = _Key(b"a" * 32)
KEY_B = _Key(b"b" * 32)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "ba_home", lambda: tmp_path)
    yield


def _path() -> str:
    return str(secret_store._path())


# --- store / read round-trip --------------------------------------------


def test_store_then_read_round_trips():
    secret_store.store_secret("ref", "super-secret", KEY_A)
    assert secret_store.read_secret("ref", KEY_A) == "super-secret"


def test_store_persists_as_ciphertext_not_plaintext():
    secret_store.store_secret("ref", "super-secret", KEY_A)
    raw = secret_store._path().read_text(encoding="utf-8")
    assert "super-secret" not in raw
    blob = json.loads(raw)
    assert set(blob["ref"]) == {"nonce", "ct"}


def test_store_is_atomic_to_0600_permissions():
    secret_store.store_secret("ref", "v", KEY_A)
    mode = stat.S_IMODE(secret_store._path().stat().st_mode)
    assert mode == 0o600


def test_store_rejects_empty_value():
    with pytest.raises(ValueError, match="non-empty"):
        secret_store.store_secret("ref", "", KEY_A)


def test_store_rejects_invalid_ref():
    with pytest.raises(ValueError, match="invalid secret_ref"):
        secret_store.store_secret("bad ref!", "v", KEY_A)


def test_read_unknown_ref_raises_keyerror():
    with pytest.raises(KeyError):
        secret_store.read_secret("nope", KEY_A)


def test_read_rejects_invalid_ref():
    with pytest.raises(ValueError, match="invalid secret_ref"):
        secret_store.read_secret("bad/ref", KEY_A)


def test_read_with_wrong_key_fails():
    secret_store.store_secret("ref", "super-secret", KEY_A)
    with pytest.raises(InvalidTag):
        secret_store.read_secret("ref", KEY_B)


def test_ciphertext_binds_ref_as_aad():
    # The secret_ref is AES-GCM associated data: decrypting the stored
    # ciphertext under a DIFFERENT ref must fail authentication.
    secret_store.store_secret("ref-a", "value", KEY_A)
    rec = json.loads(secret_store._path().read_text(encoding="utf-8"))["ref-a"]
    nonce = bytes.fromhex(rec["nonce"])
    ct = bytes.fromhex(rec["ct"])
    with pytest.raises(InvalidTag):
        AESGCM(KEY_A.master_key()).decrypt(nonce, ct, b"ref-b")


def test_store_overwrite_preserves_other_entries():
    secret_store.store_secret("a", "1", KEY_A)
    secret_store.store_secret("b", "2", KEY_A)
    secret_store.store_secret("a", "1-updated", KEY_A)
    assert secret_store.read_secret("a", KEY_A) == "1-updated"
    assert secret_store.read_secret("b", KEY_A) == "2"


# --- delete --------------------------------------------------------------


def test_delete_existing_returns_true_and_removes():
    secret_store.store_secret("ref", "v", KEY_A)
    assert secret_store.delete_secret("ref") is True
    assert not secret_store.has_secret("ref")
    with pytest.raises(KeyError):
        secret_store.read_secret("ref", KEY_A)


def test_delete_missing_returns_false():
    assert secret_store.delete_secret("nope") is False


def test_delete_rejects_invalid_ref():
    with pytest.raises(ValueError, match="invalid secret_ref"):
        secret_store.delete_secret("bad ref!")


# --- has_secret / list_refs ---------------------------------------------


def test_has_secret_true_after_store_false_otherwise():
    assert secret_store.has_secret("ref") is False
    secret_store.store_secret("ref", "v", KEY_A)
    assert secret_store.has_secret("ref") is True


def test_has_secret_invalid_ref_returns_false():
    assert secret_store.has_secret("bad ref!") is False


def test_list_refs_sorted():
    secret_store.store_secret("charlie", "1", KEY_A)
    secret_store.store_secret("alpha", "2", KEY_A)
    secret_store.store_secret("bravo", "3", KEY_A)
    assert secret_store.list_refs() == ["alpha", "bravo", "charlie"]


def test_list_refs_empty_when_no_store():
    assert secret_store.list_refs() == []


# --- _require_ref regex boundaries --------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["a", "A", "1", "_", "-", "abc_DEF-123", "a" * 128],
)
def test_ref_valid_accepted(ref):
    secret_store.store_secret(ref, "v", KEY_A)
    assert secret_store.has_secret(ref)


@pytest.mark.parametrize(
    "ref",
    ["", "a.b", "a/b", "a b", "a!", "café", "a" * 129],
)
def test_ref_invalid_rejected(ref):
    with pytest.raises(ValueError):
        secret_store.store_secret(ref, "v", KEY_A)
    assert secret_store.has_secret(ref) is False


# --- _load_raw resilience ------------------------------------------------


def test_load_raw_returns_empty_when_corrupt_json():
    secret_store._dir()  # ensure credential_broker dir exists
    secret_store._path().write_text("{not json", encoding="utf-8")
    # corrupt store is treated as empty by every reader
    assert secret_store.list_refs() == []
    assert secret_store.has_secret("ref") is False
    assert secret_store.delete_secret("ref") is False
    with pytest.raises(KeyError):
        secret_store.read_secret("ref", KEY_A)


def test_load_raw_returns_empty_on_oserror(tmp_path, monkeypatch):
    # secrets.enc exists but is a directory -> read_text raises
    # IsADirectoryError (OSError subclass) -> swallowed -> treated as empty.
    secret_store._dir()
    secret_store._path().mkdir()
    assert secret_store.list_refs() == []
    assert secret_store.has_secret("ref") is False


def test_store_propagates_oserror_when_home_not_a_dir(tmp_path, monkeypatch):
    # ba_home pointing at a regular file makes _dir().mkdir raise
    # (FileExistsError / NotADirectoryError, both OSError): the store path
    # fails CLOSED — no silent swallow, no fallback. Portable on host and
    # Docker(root) since root cannot turn a file into a directory either.
    file_home = tmp_path / "iam-a-file"
    file_home.write_text("x", encoding="utf-8")
    monkeypatch.setattr(secret_store, "ba_home", lambda: file_home)
    with pytest.raises(OSError):
        secret_store.store_secret("ref", "v", KEY_A)


# --- file lives under credential_broker/ ---------------------------------


def test_store_writes_under_credential_broker_subdir():
    secret_store.store_secret("ref", "v", KEY_A)
    home = secret_store.ba_home()
    assert (_path()).startswith(str(home / "credential_broker"))
