"""Dedicated pytest coverage for setup_nonce.py.

The legacy `test_setup_nonce_security.py` doubles as an integration check
(app boot + auth route) and only covers the happy path under pytest; it sat at
~84% (missing the fsync-failure reraise, the insecure-mode `_read` guard, and
the consume-expiry branch). This file covers every branch deterministically.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Isolate BEFORE importing backend modules: setup_nonce writes to ba_home().
import _test_home

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

import setup_nonce  # noqa: E402


@pytest.fixture
def home():
    """Fresh isolated state home per test; released on teardown."""
    handle = _test_home.TestHome.acquire(prefix="ba-test-setup-nonce-")
    yield handle.path
    handle.release()


def _write_nonce_file(payload: object, mode: int = 0o600) -> None:
    path = setup_nonce._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    os.chmod(path, mode)


# --- mint / consume happy path --------------------------------------------

def test_mint_writes_mode_0600_and_consume_succeeds(home):
    nonce = setup_nonce.mint()
    assert len(nonce) > 0
    st = setup_nonce._path().stat()
    assert st.st_mode & 0o777 == 0o600
    assert setup_nonce.consume(nonce) is True
    # single-use: file removed after success
    assert not setup_nonce._path().exists()


def test_consume_strips_whitespace(home):
    nonce = setup_nonce.mint()
    assert setup_nonce.consume(f"  {nonce}  ") is True


# --- consume falsy-input guards -------------------------------------------

@pytest.mark.parametrize("candidate", [None, "", "   ", "\t"])
def test_consume_empty_returns_false(home, candidate):
    assert setup_nonce.consume(candidate) is False


def test_consume_no_file_returns_false(home):
    assert setup_nonce.consume("anything") is False


# --- consume expiry --------------------------------------------------------

def test_consume_expired_unlinks_and_returns_false(home, monkeypatch):
    monkeypatch.setattr(setup_nonce.time, "time", lambda: 1000.0)
    nonce = setup_nonce.mint()  # expires_at = 1600.0
    monkeypatch.setattr(setup_nonce.time, "time", lambda: 1601.0)
    assert setup_nonce.consume(nonce) is False
    assert not setup_nonce._path().exists()


def test_consume_wrong_value_keeps_file_and_returns_false(home):
    nonce = setup_nonce.mint()
    assert setup_nonce.consume("wrong") is False
    # mismatch does NOT consume the file
    assert setup_nonce._path().exists()
    assert setup_nonce.consume(nonce) is True


# --- _read guards ----------------------------------------------------------

def test_read_rejects_insecure_file_mode(home):
    nonce = setup_nonce.mint()
    os.chmod(setup_nonce._path(), 0o644)  # group/other bits set
    assert setup_nonce._read() is None
    assert setup_nonce.consume(nonce) is False


def test_read_rejects_non_dict_json(home):
    _write_nonce_file([1, 2, 3])  # valid json, not a dict
    assert setup_nonce._read() is None


def test_read_rejects_corrupt_json(home):
    _write_nonce_file("not-json{")
    assert setup_nonce._read() is None


# --- mint fsync-failure reraise -------------------------------------------

def test_mint_fsync_failure_reraises_and_leaves_no_target(home, monkeypatch):
    def boom(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(setup_nonce.os, "fsync", boom)

    with pytest.raises(OSError, match="simulated fsync failure"):
        setup_nonce.mint()

    # os.replace never ran, so the canonical nonce path must not exist
    assert not setup_nonce._path().exists()
