from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import qr_auth  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #

def _seed_state(state: dict) -> None:
    """Write a state file directly, bypassing _write() (so we can plant modes
    and shapes _write() would normalize away)."""
    path = qr_auth._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _read_state_file() -> dict:
    path = qr_auth._path()
    return json.loads(path.read_text(encoding="utf-8"))


def test_now_returns_real_wall_clock():
    # Exercises the unpatched _now() body (no frozen_now fixture) so its
    # `return time.time()` line is covered, not just the patched lambda.
    import time

    before = time.time()
    value = qr_auth._now()
    after = time.time()
    assert before <= value <= after


@pytest.fixture
def frozen_now(monkeypatch):
    """Control qr_auth's clock. Returns a setter."""
    t = [1_000_000.0]

    def set_now(value: float):
        t[0] = value

    monkeypatch.setattr(qr_auth, "_now", lambda: t[0])
    return set_now


@pytest.fixture(autouse=True)
def stub_access_token(monkeypatch):
    """Make auth.create_access_token deterministic — qr_auth only stores opaque
    tokens, so the JWT shape is irrelevant to the behavior under test."""
    monkeypatch.setattr(qr_auth.auth, "create_access_token", lambda sub: f"access::{sub}")


@pytest.fixture(autouse=True)
def clean_state():
    """Start every test with no state file."""
    path = qr_auth._path()
    if path.exists():
        path.unlink()
    yield
    if path.exists():
        path.unlink()


# --------------------------------------------------------------------------- #
# _read
# --------------------------------------------------------------------------- #


def test_read_missing_file_returns_empty():
    state = qr_auth._read()
    assert state == {"grants": {}, "families": {}}


def test_read_garbage_returns_empty():
    qr_auth._path().parent.mkdir(parents=True, exist_ok=True)
    qr_auth._path().write_text("not json{", encoding="utf-8")
    assert qr_auth._read() == {"grants": {}, "families": {}}


def test_read_non_dict_returns_empty():
    _seed_state(["not", "a", "dict"])  # type: ignore[arg-type]
    assert qr_auth._read() == {"grants": {}, "families": {}}


def test_read_world_readable_returns_empty():
    # File with group/other bits set is untrusted → treated as empty.
    path = qr_auth._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"grants": {"x": 1}}), encoding="utf-8")
    os.chmod(path, 0o644)
    assert qr_auth._read() == {"grants": {}, "families": {}}


def test_read_dict_msetdefault():
    _seed_state({"grants": {"g1": 9_999}})
    state = qr_auth._read()
    assert state["grants"] == {"g1": 9_999}
    assert state["families"] == {}


def test_read_dict_families_setdefault():
    _seed_state({})
    state = qr_auth._read()
    assert state == {"grants": {}, "families": {}}


def test_read_full_dict_passes_through():
    payload = {"grants": {"g": 5}, "families": {"f": {"jti": "j", "sub": "s", "exp": 5}}}
    _seed_state(payload)
    assert qr_auth._read() == payload


# --------------------------------------------------------------------------- #
# _prune
# --------------------------------------------------------------------------- #


def test_prune_drops_expired(frozen_now):
    frozen_now(1000.0)
    state = {
        "grants": {"live": 2000.0, "dead": 500.0},
        "families": {
            "alive": {"jti": "j1", "sub": "s", "exp": 2000.0},
            "gone": {"jti": "j2", "sub": "s", "exp": 500.0},
        },
    }
    qr_auth._prune(state)
    assert state["grants"] == {"live": 2000.0}
    assert set(state["families"]) == {"alive"}


def test_prune_missing_exp_treated_as_expired(frozen_now):
    frozen_now(1000.0)
    state = {"grants": {}, "families": {"noexp": {"jti": "j", "sub": "s"}}}
    qr_auth._prune(state)
    assert state["families"] == {}


# --------------------------------------------------------------------------- #
# _write
# --------------------------------------------------------------------------- #


def test_write_roundtrip_and_mode(frozen_now):
    frozen_now(1000.0)
    qr_auth._write({"grants": {"g": 2000.0}, "families": {}})
    path = qr_auth._path()
    assert path.exists()
    assert (path.stat().st_mode & 0o077) == 0  # 0600, no group/other bits
    assert _read_state_file()["grants"] == {"g": 2000.0}


def test_write_creates_parent_dir(tmp_path, monkeypatch, frozen_now):
    frozen_now(1000.0)
    # Point ba_home at a not-yet-existing dir to exercise mkdir(parents=True).
    nested = tmp_path / "deep" / "home"
    monkeypatch.setattr(qr_auth, "ba_home", lambda: nested)
    qr_auth._write({"grants": {}, "families": {}})
    assert (nested / "qr_auth_state.json").exists()


def test_write_failure_closes_fd_and_reraises(frozen_now):
    frozen_now(1000.0)
    # Put the non-serializable value in an EXTRA key so _prune (which only
    # inspects grants/families) succeeds, then json.dump raises INSIDE the
    # fdopen `with`. fd has been adopted by the file object, so the except's
    # os.close(fd) raises OSError (caught) and the original error re-raises.
    with pytest.raises(TypeError):
        qr_auth._write({"grants": {}, "families": {}, "weird": object()})  # type: ignore[dict-item]
    # The failed write must not have committed a state file (os.replace never ran).
    assert not qr_auth._path().exists()


# --------------------------------------------------------------------------- #
# mint_grant / consume_grant
# --------------------------------------------------------------------------- #


def test_mint_grant_persists(frozen_now):
    frozen_now(1000.0)
    token = qr_auth.mint_grant()
    assert token
    state = _read_state_file()
    assert state["grants"][token] == 1000.0 + qr_auth.GRANT_TTL


def test_consume_grant_empty_inputs():
    for bad in (None, "", "   "):
        assert qr_auth.consume_grant(bad) is False


def test_consume_grant_unknown_does_not_write(frozen_now):
    frozen_now(1000.0)
    # Pre-seed a real grant so the state file exists.
    qr_auth.mint_grant()
    mtime_before = qr_auth._path().stat().st_mtime_ns
    assert qr_auth.consume_grant("totally-bogus") is False
    # Unknown grant path must NOT fsync/rewrite the state file.
    assert qr_auth._path().stat().st_mtime_ns == mtime_before


def test_consume_grant_valid(frozen_now):
    frozen_now(1000.0)
    token = qr_auth.mint_grant()
    assert qr_auth.consume_grant(token) is True
    # Single-use: gone from state, second redeem fails.
    assert token not in _read_state_file()["grants"]
    assert qr_auth.consume_grant(token) is False


def test_consume_grant_expired(frozen_now):
    frozen_now(1000.0)
    token = qr_auth.mint_grant()
    frozen_now(1000.0 + qr_auth.GRANT_TTL + 1)
    assert qr_auth.consume_grant(token) is False
    # Expired grant is still popped + state written.
    assert token not in _read_state_file()["grants"]


# --------------------------------------------------------------------------- #
# revoke_all_sessions
# --------------------------------------------------------------------------- #


def test_revoke_all_sessions_wipes(frozen_now):
    frozen_now(1000.0)
    qr_auth.mint_grant()
    qr_auth.issue_session("user-1")
    assert _read_state_file()["grants"]
    assert _read_state_file()["families"]
    qr_auth.revoke_all_sessions()
    state = _read_state_file()
    assert state["grants"] == {}
    assert state["families"] == {}


# --------------------------------------------------------------------------- #
# issue_session / rotate
# --------------------------------------------------------------------------- #


def test_issue_session_returns_tokens(frozen_now):
    frozen_now(1000.0)
    access, refresh = qr_auth.issue_session("user-1")
    assert access == "access::user-1"
    fam, _, jti = refresh.partition(".")
    assert fam and jti
    rec = _read_state_file()["families"][fam]
    assert rec["sub"] == "user-1"
    assert rec["jti"] == jti
    assert rec["exp"] == 1000.0 + qr_auth.REFRESH_TTL


def test_rotate_malformed_tokens_return_none():
    for bad in (None, "", "   ", "no-dot", ".leadinglempty"):
        assert qr_auth.rotate(bad) is None


def test_rotate_unknown_family_no_write(frozen_now):
    frozen_now(1000.0)
    qr_auth.issue_session("user-1")  # creates the state file
    mtime_before = qr_auth._path().stat().st_mtime_ns
    assert qr_auth.rotate("unknownfam.unknownjti") is None
    # Unknown family must NOT fsync/rewrite the state file.
    assert qr_auth._path().stat().st_mtime_ns == mtime_before


def test_rotate_expired_family(frozen_now):
    frozen_now(1000.0)
    _, refresh = qr_auth.issue_session("user-1")
    fam = refresh.partition(".")[0]
    assert fam in _read_state_file()["families"]
    frozen_now(1000.0 + qr_auth.REFRESH_TTL + 1)
    assert qr_auth.rotate(refresh) is None
    # Expired family is dropped on the write.
    assert fam not in _read_state_file()["families"]


def test_rotate_replay_revokes_family(frozen_now):
    frozen_now(1000.0)
    _, refresh = qr_auth.issue_session("user-1")
    fam = refresh.partition(".")[0]

    # Legitimate rotation: old refresh exchanged for a new one.
    access_new, refresh_new = qr_auth.rotate(refresh)
    assert access_new == "access::user-1"
    assert refresh_new != refresh
    assert refresh_new.partition(".")[0] == fam  # same family, new jti

    # Replaying the already-rotated token is treated as theft → family revoked.
    assert qr_auth.rotate(refresh) is None
    assert fam not in _read_state_file()["families"]
    # And the legitimate new token now fails too (forced re-onboard).
    assert qr_auth.rotate(refresh_new) is None


def test_rotate_valid_returns_new_pair(frozen_now):
    frozen_now(1000.0)
    _, refresh = qr_auth.issue_session("user-1")
    access, refresh_new = qr_auth.rotate(refresh)
    assert access == "access::user-1"
    assert refresh_new != refresh
    rec = _read_state_file()["families"][refresh.partition(".")[0]]
    assert rec["jti"] == refresh_new.partition(".")[2]
    # Sliding expiry pushed forward to the new now.
    assert rec["exp"] == 1000.0 + qr_auth.REFRESH_TTL


def test_rotate_valid_advances_sliding_expiry(frozen_now):
    frozen_now(1000.0)
    _, refresh = qr_auth.issue_session("user-1")
    frozen_now(2000.0)  # advance clock; rotation should slide expiry to 2000+TTL
    _, _ = qr_auth.rotate(refresh)
    rec = _read_state_file()["families"][refresh.partition(".")[0]]
    assert rec["exp"] == 2000.0 + qr_auth.REFRESH_TTL
