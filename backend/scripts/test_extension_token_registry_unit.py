"""Dedicated unit owner for extension_token_registry.

This module owns the internal-loopback token registry: stable per-extension
secrets persisted under ba_home(), reverse-mapped with a constant-time
compare, cached behind a fingerprint+TTL guard that invalidates on an
out-of-process write or a ba_home switch. The registry is the auth backbone
that lets the backend derive extension identity from a token instead of a
self-asserted header, so every branch here is a real security property.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extension_token_registry as etr  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """Drop the module cache + token file so each test starts from a clean home.

    The conftest already engages an isolated per-module ba_home(); we only need
    to forget whatever a prior test cached and remove any persisted registry.
    """
    etr._cache = None
    etr._cache_key = None
    etr._last_fingerprint_check = 0.0
    path = etr._path()
    tmp = path.with_suffix(".tmp")
    for p in (path, tmp):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    yield
    etr._cache = None
    etr._cache_key = None
    etr._last_fingerprint_check = 0.0


# --- _path ---

def test_path_is_under_ba_home() -> None:
    import paths

    assert etr._path() == paths.ba_home() / "extension_tokens.json"


# --- _fingerprint ---

def test_fingerprint_of_existing_file_is_path_plus_sha256() -> None:
    path = etr._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"some-bytes"
    path.write_bytes(payload)
    import hashlib

    fp = etr._fingerprint(path)
    assert fp == (str(path), hashlib.sha256(payload).hexdigest())


def test_fingerprint_of_missing_file_has_empty_digest() -> None:
    path = etr._path()
    assert not path.exists()
    assert etr._fingerprint(path) == (str(path), "")


# --- _load_locked: parsing + coercion ---

def test_load_missing_file_returns_empty() -> None:
    assert etr._load_locked() == {}


def test_load_non_dict_json_returns_empty() -> None:
    path = etr._path()
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert etr._load_locked() == {}


def test_load_coerces_non_str_values_to_str() -> None:
    path = etr._path()
    path.write_text(json.dumps({"ext-a": 123, "ext-b": True}), encoding="utf-8")
    assert etr._load_locked() == {"ext-a": "123", "ext-b": "True"}


def test_load_malformed_json_returns_empty() -> None:
    path = etr._path()
    path.write_text("{not json", encoding="utf-8")
    assert etr._load_locked() == {}


def test_load_oserror_returns_empty() -> None:
    path = etr._path()
    path.write_text(json.dumps({"ext": "tok"}), encoding="utf-8")
    real_read_text = Path.read_text

    def failing_read(self, *a, **k):
        raise OSError("simulated")

    Path.read_text = failing_read  # type: ignore[method-assign]
    try:
        assert etr._load_locked() == {}
    finally:
        Path.read_text = real_read_text  # type: ignore[method-assign]


# --- _load_locked: cache fast paths ---

def test_load_within_ttl_serves_stale_cache_without_reread() -> None:
    path = etr._path()
    path.write_text(json.dumps({"ext": "first"}), encoding="utf-8")
    assert etr._load_locked() == {"ext": "first"}
    # Out-of-process write AFTER the cache was populated, within the TTL window.
    path.write_text(json.dumps({"ext": "second", "ghost": "x"}), encoding="utf-8")
    got = etr._load_locked()
    # TTL fast path: cached value returned, the on-disk change is invisible.
    assert got == {"ext": "first"}
    assert "ghost" not in got


def test_load_fingerprint_match_returns_cache_without_reparsing() -> None:
    path = etr._path()
    path.write_text(json.dumps({"ext": "tok"}), encoding="utf-8")
    etr._load_locked()  # populate cache + fingerprint
    etr._last_fingerprint_check = 0.0  # force TTL expiry, keep fingerprint stable
    parse_calls = 0
    real_loads = etr.json.loads

    def counting_loads(*a, **k):
        nonlocal parse_calls
        parse_calls += 1
        return real_loads(*a, **k)

    etr.json.loads = counting_loads  # type: ignore[attr-defined]
    try:
        got = etr._load_locked()
    finally:
        etr.json.loads = real_loads  # type: ignore[attr-defined]
    assert got == {"ext": "tok"}
    assert parse_calls == 0  # fingerprint matched → served cache, no disk parse


def test_load_fingerprint_mismatch_reloads_from_disk() -> None:
    path = etr._path()
    path.write_text(json.dumps({"ext": "old"}), encoding="utf-8")
    etr._load_locked()
    etr._last_fingerprint_check = 0.0  # TTL expired
    path.write_text(json.dumps({"ext": "new"}), encoding="utf-8")  # fingerprint changes
    assert etr._load_locked() == {"ext": "new"}


# --- _persist_locked ---

def test_persist_writes_restricted_atomically_and_cleans_tmp() -> None:
    path = etr._path()
    etr._persist_locked({"ext": "tok"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ext": "tok"}
    assert not path.with_suffix(".tmp").exists()
    if os.name != "nt":
        assert (path.stat().st_mode & 0o077) == 0


def test_persist_tolerates_chmod_oserror() -> None:
    real_chmod = etr.os.chmod

    def always_fails(_path, _mode):
        raise OSError("chmod denied")

    etr.os.chmod = always_fails  # type: ignore[attr-defined]
    try:
        etr._persist_locked({"ext": "tok"})
    finally:
        etr.os.chmod = real_chmod  # type: ignore[attr-defined]
    # Both chmod calls (tmp + final) are swallowed; the replace still landed.
    assert json.loads(etr._path().read_text(encoding="utf-8")) == {"ext": "tok"}


# --- mint ---

def test_mint_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        etr.mint("")


def test_mint_rejects_whitespace_only_id() -> None:
    with pytest.raises(ValueError):
        etr.mint("   \t ")


def test_mint_creates_and_persists_token() -> None:
    token = etr.mint("ext-a")
    assert token
    on_disk = json.loads(etr._path().read_text(encoding="utf-8"))
    assert on_disk == {"ext-a": token}


def test_mint_is_stable_across_calls() -> None:
    first = etr.mint("ext-a")
    second = etr.mint("ext-a")
    assert first == second  # stability is the whole point — no rotation on reuse


def test_mint_distinct_ids_get_distinct_tokens() -> None:
    a = etr.mint("ext-a")
    b = etr.mint("ext-b")
    assert a != b


def test_mint_survives_cache_reset() -> None:
    token = etr.mint("ext-a")
    etr._cache = None
    etr._cache_key = None
    assert etr.mint("ext-a") == token  # persisted, reloaded from disk


# --- resolve ---

def test_resolve_none_returns_none() -> None:
    assert etr.resolve(None) is None


def test_resolve_empty_returns_none() -> None:
    assert etr.resolve("") is None


def test_resolve_round_trips_a_minted_token() -> None:
    token = etr.mint("ext-a")
    assert etr.resolve(token) == "ext-a"


def test_resolve_unknown_token_returns_none() -> None:
    etr.mint("ext-a")
    assert etr.resolve("not-a-real-token") is None


def test_resolve_picks_correct_extension_among_many() -> None:
    a = etr.mint("ext-a")
    b = etr.mint("ext-b")
    assert etr.resolve(a) == "ext-a"
    assert etr.resolve(b) == "ext-b"


# --- resolve_fresh ---

def test_resolve_fresh_picks_up_out_of_process_mint() -> None:
    etr.mint("ext-a")  # populate cache
    # Another process mints ext-b directly on disk after our cache was built.
    path = etr._path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ext-b"] = "fresh-token-from-elsewhere"
    path.write_text(json.dumps(data), encoding="utf-8")
    # Stale resolve still works for the cached id...
    assert etr.resolve(etr.mint("ext-a")) == "ext-a"
    # ...but only resolve_fresh sees the out-of-process addition.
    assert etr.resolve("fresh-token-from-elsewhere") is None
    assert etr.resolve_fresh("fresh-token-from-elsewhere") == "ext-b"


# --- revoke ---

def test_revoke_empty_id_is_noop() -> None:
    etr.mint("ext-a")
    before = etr._path().read_text(encoding="utf-8")
    etr.revoke("   ")
    assert etr._path().read_text(encoding="utf-8") == before  # unchanged, no rewrite


def test_revoke_existing_id_removes_token() -> None:
    token = etr.mint("ext-a")
    etr.mint("ext-b")
    etr.revoke("ext-a")
    assert etr.resolve(token) is None
    assert "ext-a" not in json.loads(etr._path().read_text(encoding="utf-8"))


def test_revoke_missing_id_is_noop() -> None:
    etr.mint("ext-a")
    before = etr._path().read_text(encoding="utf-8")
    etr.revoke("never-minted")
    assert etr._path().read_text(encoding="utf-8") == before


# --- extension_ids ---

def test_extension_ids_returns_minted_set() -> None:
    etr.mint("ext-a")
    etr.mint("ext-b")
    assert etr.extension_ids() == {"ext-a", "ext-b"}


def test_extension_ids_empty_when_nothing_minted() -> None:
    assert etr.extension_ids() == set()


# --- end-to-end security round-trip ---

def test_token_identity_survives_revoke_and_reissue() -> None:
    first = etr.mint("ext-a")
    assert etr.resolve(first) == "ext-a"
    etr.revoke("ext-a")
    assert etr.resolve(first) is None  # revoked token no longer authenticates
    second = etr.mint("ext-a")  # re-issue after revoke
    assert second != first  # reissue is a fresh secret
    assert etr.resolve(second) == "ext-a"
    assert etr.resolve(first) is None  # old token stays dead
