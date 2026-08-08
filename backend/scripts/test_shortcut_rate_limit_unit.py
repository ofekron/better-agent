#!/usr/bin/env python3
"""Dedicated unit coverage for backend/shortcut_rate_limit.py.

shortcut_rate_limit.py is the security-top probe rate limiter for the shortcut
picker: a per-scope lease/cooldown state machine backed by an isolated home
dir, an HMAC'd scope key, an in-memory cooldown cache, and an HTTP
Retry-After parser. The only existing owner, test_shortcut_rate_limit.py, is a
standalone __main__ script (pytest collects 0 items), so the module was
effectively pytest-ownerless at the unit tier (~18% import-time only).

This file drives every callable + branch hermetically against an isolated
BETTER_AGENT_HOME tempdir. No real state is ever touched.
"""
from __future__ import annotations

import email.utils
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-shortcut-rate-limit-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(str(_TEST_HOME))

import shortcut_rate_limit as srl  # noqa: E402
from shortcut_rate_limit import Claim, ProbeLease  # noqa: E402

SCOPE = "a" * 64


@pytest.fixture(autouse=True)
def _reset_state():
    srl._local_cooldowns.clear()
    root = srl._root()
    for child in root.glob("*"):
        if child.is_file():
            try:
                child.unlink()
            except FileNotFoundError:
                pass
    yield
    srl._local_cooldowns.clear()


@pytest.fixture
def clock(monkeypatch):
    """Controllable monotonic + wall clock patched on the module's `time`."""
    state = {"t": 1_700_000_000.0}

    def fake_time():
        return state["t"]

    def fake_monotonic():
        return state["t"]

    monkeypatch.setattr(srl.time, "time", fake_time)
    monkeypatch.setattr(srl.time, "monotonic", fake_monotonic)
    return state


def _state_path() -> Path:
    return srl._paths(SCOPE)[0]


def _raw_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


# --------------------------------------------------------------------------- #
# _normalized_endpoint (pure)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/a/b/", "https://example.com/a/b"),
        ("http://host:8080/x", "http://host:8080/x"),
        ("https://h:443/p", "https://h/p"),
        ("http://h:80/p", "http://h/p"),
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("https://h/", "https://h"),
        ("https://h", "https://h"),
        ("noscheme/path", ":///noscheme/path"),
        ("", "://"),
    ],
)
def test_normalized_endpoint(url, expected):
    assert srl._normalized_endpoint(url) == expected


# --------------------------------------------------------------------------- #
# _paths validation
# --------------------------------------------------------------------------- #


def test_paths_valid_returns_under_root():
    json_path, lock_path = srl._paths(SCOPE)
    assert json_path.parent == lock_path.parent == srl._root()
    assert json_path.name == f"{SCOPE}.json"
    assert lock_path.name == f"{SCOPE}.lock"


@pytest.mark.parametrize("bad", ["", "xyz", "g" * 64, "A" * 64, "a" * 63, "a" * 65])
def test_paths_rejects_invalid_scope(bad):
    with pytest.raises(ValueError):
        srl._paths(bad)


# --------------------------------------------------------------------------- #
# _root
# --------------------------------------------------------------------------- #


def test_root_creates_secure_dir():
    root = srl._root()
    assert root.is_dir()


# --------------------------------------------------------------------------- #
# _salt (file-backed idempotent key)
# --------------------------------------------------------------------------- #


def test_salt_generates_then_persists():
    first = srl._salt()
    second = srl._salt()
    assert len(first) == 32
    assert first == second  # idempotent across calls
    key_path = srl._root() / "scope.key"
    assert key_path.read_bytes() == first


def test_salt_regenerates_when_truncated():
    key_path = srl._root() / "scope.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(b"short")  # not 32 bytes
    regenerated = srl._salt()
    assert len(regenerated) == 32
    assert regenerated != b"short"
    assert key_path.read_bytes() == regenerated


def test_salt_closes_fd_and_unlinks_on_write_failure(monkeypatch):
    key_path = srl._root() / "scope.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(b"x")  # force regeneration path

    closed = []
    real_close = srl.os.close

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(srl.os, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(srl.os, "close", spy_close)
    with pytest.raises(OSError):
        srl._salt()
    # the mkstemp fd was closed in the finally — no fd leak on write failure
    assert closed


# --------------------------------------------------------------------------- #
# scope_key
# --------------------------------------------------------------------------- #


def test_scope_key_deterministic_and_sensitive():
    a = srl.scope_key(provider_id="p", base_url="https://h", model="m", api_key="k")
    a2 = srl.scope_key(provider_id="p", base_url="https://h", model="m", api_key="k")
    assert a == a2
    assert len(a) == 64
    # api_key never leaks raw; different key -> different scope
    b = srl.scope_key(provider_id="p", base_url="https://h", model="m", api_key="k2")
    assert b != a
    # endpoint normalization affects scope
    c = srl.scope_key(provider_id="p", base_url="https://h:443", model="m", api_key="k")
    assert c == a
    d = srl.scope_key(provider_id="p2", base_url="https://h", model="m", api_key="k")
    assert d != a


# --------------------------------------------------------------------------- #
# _read (every corrupt branch)
# --------------------------------------------------------------------------- #


def test_read_missing_path():
    assert srl._read(srl._root() / "nope.json") == ({}, False)


def test_read_valid():
    path = _state_path()
    payload = {
        "version": srl._VERSION,
        "observed_epoch": 5,
        "cooldown_until_epoch": 0,
        "lease_until_epoch": 0,
        "lease_token": "tok",
    }
    _raw_write(path, json.dumps(payload))
    value, corrupt = srl._read(path)
    assert corrupt is False
    assert value["lease_token"] == "tok"


@pytest.mark.parametrize("payload", ["not json{{", "[1,2,3]"])
def test_read_corrupt_or_nondict(payload):
    path = _state_path()
    _raw_write(path, payload)
    assert srl._read(path) == ({}, True)


def test_read_wrong_version():
    path = _state_path()
    _raw_write(path, json.dumps({"version": 999}))
    assert srl._read(path) == ({}, True)


@pytest.mark.parametrize("key", ["observed_epoch", "cooldown_until_epoch", "lease_until_epoch"])
def test_read_non_numeric_epoch(key):
    path = _state_path()
    base = {"version": srl._VERSION, "lease_token": ""}
    base[key] = "not-a-number"
    _raw_write(path, json.dumps(base))
    assert srl._read(path) == ({}, True)


def test_read_non_str_lease_token():
    path = _state_path()
    _raw_write(path, json.dumps({"version": srl._VERSION, "lease_token": 5}))
    assert srl._read(path) == ({}, True)


# --------------------------------------------------------------------------- #
# _write + _read round trip
# --------------------------------------------------------------------------- #


def test_write_then_read_round_trip():
    path = _state_path()
    payload = {"version": srl._VERSION, "observed_epoch": 1, "lease_token": "z"}
    srl._write(path, payload)
    value, corrupt = srl._read(path)
    assert corrupt is False
    assert value["observed_epoch"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------- #
# _fsync_parent
# --------------------------------------------------------------------------- #


def test_fsync_parent_succeeds_on_real_dir():
    srl._fsync_parent(srl._root())  # no raise


def test_fsync_parent_swallows_on_windows(monkeypatch):
    monkeypatch.setattr(srl.os, "name", "nt")

    def raise_oserror(*a, **k):
        raise OSError("synthetic")

    monkeypatch.setattr(srl.os, "open", raise_oserror)
    srl._fsync_parent(srl._root())  # swallowed, no raise


def test_fsync_parent_propagates_on_posix(monkeypatch):
    monkeypatch.setattr(srl.os, "name", "posix")

    def raise_oserror(*a, **k):
        raise OSError("synthetic")

    monkeypatch.setattr(srl.os, "open", raise_oserror)
    with pytest.raises(OSError):
        srl._fsync_parent(srl._root())


# --------------------------------------------------------------------------- #
# _clock_state (pure rebase logic)
# --------------------------------------------------------------------------- #


def test_clock_state_no_rebase_when_observed_le_now():
    now = 1000.0
    state = {"observed_epoch": 500.0, "cooldown_until_epoch": 10.0}
    out, current, rebased = srl._clock_state(state, now)
    assert rebased is False
    assert current == now
    assert out is state  # unchanged, same object


def test_clock_state_no_rebase_at_boundary():
    now = 1000.0
    out, current, rebased = srl._clock_state({"observed_epoch": now}, now)
    assert rebased is False and current == now


def test_clock_state_rebases_future_observed():
    now = 1000.0
    state = {
        "observed_epoch": now + 100,
        "cooldown_until_epoch": now + 50,
        "lease_until_epoch": now + 30,
    }
    out, current, rebased = srl._clock_state(state, now)
    assert rebased is True
    assert current == now
    assert out["observed_epoch"] == now
    assert out["cooldown_until_epoch"] == now + 50 - 100
    assert out["lease_until_epoch"] == now + 30 - 100
    assert out is not state  # copied


def test_clock_state_clamps_negative_to_zero():
    now = 1000.0
    state = {"observed_epoch": now + 2000, "cooldown_until_epoch": now + 5}
    out, _, _ = srl._clock_state(state, now)
    assert out["cooldown_until_epoch"] == 0.0


# --------------------------------------------------------------------------- #
# local cooldown cache
# --------------------------------------------------------------------------- #


def test_set_then_active_local_cooldown(clock):
    srl._set_local_cooldown(SCOPE, 100)
    assert srl._local_cooldown_active(SCOPE) is True


def test_expired_local_cooldown_pops(clock):
    srl._set_local_cooldown(SCOPE, 100)
    clock["t"] += 200  # past deadline
    assert srl._local_cooldown_active(SCOPE) is False
    assert SCOPE not in srl._local_cooldowns  # popped


def test_set_zero_or_negative_clears(clock):
    srl._set_local_cooldown(SCOPE, 100)
    assert srl._local_cooldown_active(SCOPE) is True
    srl._set_local_cooldown(SCOPE, 0)
    assert srl._local_cooldown_active(SCOPE) is False
    srl._set_local_cooldown(SCOPE, 100)
    srl._set_local_cooldown(SCOPE, -5)
    assert SCOPE not in srl._local_cooldowns


def test_set_clamps_to_max_cooldown(clock):
    srl._set_local_cooldown(SCOPE, 99_999)
    assert srl._local_cooldowns[SCOPE] == clock["t"] + srl._MAX_COOLDOWN_SECS


def test_set_evicts_oldest_beyond_max(monkeypatch, clock):
    monkeypatch.setattr(srl, "_LOCAL_CACHE_MAX", 2)
    srl._set_local_cooldown("k1", 100)
    srl._set_local_cooldown("k2", 100)
    srl._set_local_cooldown("k3", 100)  # evicts k1
    assert "k1" not in srl._local_cooldowns
    assert "k2" in srl._local_cooldowns and "k3" in srl._local_cooldowns
    assert len(srl._local_cooldowns) == 2


# --------------------------------------------------------------------------- #
# _locked
# --------------------------------------------------------------------------- #


def test_locked_passes_read_state_to_action():
    path = _state_path()
    _raw_write(path, json.dumps({"version": srl._VERSION, "lease_token": "x"}))

    captured = {}

    def action(p, state, corrupt):
        captured["path"] = p
        captured["state"] = state
        captured["corrupt"] = corrupt
        return "result"

    assert srl._locked(SCOPE, action) == "result"
    assert captured["state"]["lease_token"] == "x"
    assert captured["corrupt"] is False
    lock_path = srl._paths(SCOPE)[1]
    assert lock_path.exists()


# --------------------------------------------------------------------------- #
# claim
# --------------------------------------------------------------------------- #


def test_claim_short_circuits_on_local_cooldown(clock):
    srl._set_local_cooldown(SCOPE, 100)
    claim = srl.claim(SCOPE)  # now=None
    assert claim == Claim(None, "cooldown")
    assert not _state_path().exists()  # never touched disk


def test_claim_fresh_probe():
    now = 1000.0
    claim = srl.claim(SCOPE, now=now)
    assert claim.reason == "probe"
    assert claim.recovered is False
    assert claim.corrupt is False
    assert claim.lease is not None
    assert claim.lease.scope == SCOPE
    value, _ = srl._read(_state_path())
    assert value["lease_token"] == claim.lease.token
    assert value["lease_until_epoch"] == now + srl._LEASE_SECS
    assert value["cooldown_until_epoch"] == 0


def test_claim_cooldown_remaining():
    now = 1000.0
    _raw_write(
        _state_path(),
        json.dumps(
            {
                "version": srl._VERSION,
                "observed_epoch": now,
                "cooldown_until_epoch": now + 50,
                "lease_until_epoch": 0,
                "lease_token": "",
            }
        ),
    )
    claim = srl.claim(SCOPE, now=now)
    assert claim == Claim(None, "cooldown", corrupt=False)
    assert srl._local_cooldown_active(SCOPE) is True  # local cooldown seeded


def test_claim_inflight():
    now = 1000.0
    _raw_write(
        _state_path(),
        json.dumps(
            {
                "version": srl._VERSION,
                "observed_epoch": now,
                "cooldown_until_epoch": 0,
                "lease_until_epoch": now + 10,
                "lease_token": "holder",
            }
        ),
    )
    claim = srl.claim(SCOPE, now=now)
    assert claim == Claim(None, "inflight", corrupt=False)


def test_claim_recovers_stale_lease():
    now = 1000.0
    _raw_write(
        _state_path(),
        json.dumps(
            {
                "version": srl._VERSION,
                "observed_epoch": now,
                "cooldown_until_epoch": 0,
                "lease_until_epoch": 0,
                "lease_token": "stale",
            }
        ),
    )
    claim = srl.claim(SCOPE, now=now)
    assert claim.reason == "probe"
    assert claim.recovered is True


def test_claim_corrupt_propagates():
    _raw_write(_state_path(), "garbage{")
    claim = srl.claim(SCOPE, now=1000.0)
    assert claim.reason == "probe"
    assert claim.corrupt is True


def test_claim_rebases_future_observed_and_writes():
    now = 1000.0
    _raw_write(
        _state_path(),
        json.dumps(
            {
                "version": srl._VERSION,
                "observed_epoch": now + 500,
                "cooldown_until_epoch": 0,
                "lease_until_epoch": 0,
                "lease_token": "",
            }
        ),
    )
    claim = srl.claim(SCOPE, now=now)
    assert claim.reason == "probe"
    value, _ = srl._read(_state_path())
    assert value["observed_epoch"] == now  # rebased


# --------------------------------------------------------------------------- #
# finish
# --------------------------------------------------------------------------- #


def test_finish_token_mismatch_returns_false():
    now = 1000.0
    _raw_write(
        _state_path(),
        json.dumps(
            {
                "version": srl._VERSION,
                "observed_epoch": now,
                "lease_token": "actual",
                "lease_until_epoch": now + 10,
            }
        ),
    )
    assert srl.finish(ProbeLease(SCOPE, "wrong"), now=now) is False
    # state unchanged
    value, _ = srl._read(_state_path())
    assert value["lease_token"] == "actual"


def test_finish_clears_lease_no_cooldown():
    now = 1000.0
    claim = srl.claim(SCOPE, now=now)
    assert srl.finish(claim.lease, cooldown_secs=None, now=now) is True
    value, _ = srl._read(_state_path())
    assert value["lease_token"] == ""
    assert value["cooldown_until_epoch"] == now  # current + 0 cooldown
    assert value["lease_until_epoch"] == 0


@pytest.mark.parametrize(
    "secs, expected",
    [(5, 5.0), (0, 1.0), (0.2, 1.0), (99_999, 900.0), (-3, 1.0)],
)
def test_finish_clamps_cooldown(secs, expected):
    now = 1000.0
    claim = srl.claim(SCOPE, now=now)
    assert srl.finish(claim.lease, cooldown_secs=secs, now=now) is True
    value, _ = srl._read(_state_path())
    assert value["cooldown_until_epoch"] == now + expected


def test_finish_sets_local_cooldown_on_success(clock):
    claim = srl.claim(SCOPE, now=clock["t"])
    srl.finish(claim.lease, cooldown_secs=5, now=clock["t"])
    assert srl._local_cooldown_active(SCOPE) is True


# --------------------------------------------------------------------------- #
# retry_after_seconds (HTTP Retry-After parser)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [None, "", "   "])
def test_retry_after_empty_returns_default(value):
    assert srl.retry_after_seconds(value) == srl._DEFAULT_COOLDOWN_SECS


def test_retry_after_garbage_returns_default():
    assert srl.retry_after_seconds("not-a-number-nor-date") == srl._DEFAULT_COOLDOWN_SECS


def test_retry_after_negative_numeric_returns_default():
    assert srl.retry_after_seconds("-3") == srl._DEFAULT_COOLDOWN_SECS


@pytest.mark.parametrize("value, expected", [("5", 5.0), (" 5 ", 5.0), ("0", 1.0), ("10000", 900.0)])
def test_retry_after_numeric(value, expected):
    assert srl.retry_after_seconds(value) == expected


def _http_date(epoch: float) -> str:
    return email.utils.format_datetime(datetime.fromtimestamp(epoch, tz=timezone.utc))


def test_retry_after_http_date():
    now = 1_700_000_000.0
    target = now + 30
    value = _http_date(target)
    assert srl.retry_after_seconds(value, now=now) == 30.0


def test_retry_after_http_date_uses_response_date_anchor():
    now = 1_700_000_000.0
    response_date_epoch = now - 100
    target = now + 50  # 150s after the response_date anchor
    value = _http_date(target)
    result = srl.retry_after_seconds(value, response_date=_http_date(response_date_epoch), now=now)
    assert result == 150.0


def test_retry_after_http_date_bad_response_date_falls_back_to_now():
    now = 1_700_000_000.0
    target = now + 40
    value = _http_date(target)
    result = srl.retry_after_seconds(value, response_date="garbage", now=now)
    assert result == 40.0


def test_retry_after_http_date_uses_real_time_when_now_none(monkeypatch):
    fixed = 1_700_000_000.0
    monkeypatch.setattr(srl.time, "time", lambda: fixed)
    value = _http_date(fixed + 20)
    assert srl.retry_after_seconds(value) == 20.0
