"""Token-model checks for qr_auth: one-time grants + refresh rotation
with reuse detection. Runnable directly (`python test_qr_auth.py`) or via
pytest. Points BETTER_AGENT_HOME at a temp dir so it never touches real
state."""

import os
import tempfile

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="qr_auth_test_")

import auth
import qr_auth


def test_grant_is_single_use():
    g = qr_auth.mint_grant()
    assert qr_auth.consume_grant(g) is True      # first redemption works
    assert qr_auth.consume_grant(g) is False     # replay rejected
    assert qr_auth.consume_grant("bogus") is False
    assert qr_auth.consume_grant("") is False


def test_access_token_round_trips():
    access, _ = qr_auth.issue_session("alice")
    assert auth.verify_token(access) == {"username": "alice"}


def test_refresh_rotates_and_invalidates_old():
    _, r1 = qr_auth.issue_session("alice")
    out = qr_auth.rotate(r1)
    assert out is not None
    access2, r2 = out
    assert r2 != r1
    assert auth.verify_token(access2) == {"username": "alice"}
    # Replaying the rotated-away token is treated as theft → family revoked,
    # so even the current valid token stops working (forces re-onboarding).
    assert qr_auth.rotate(r1) is None
    assert qr_auth.rotate(r2) is None


def test_malformed_refresh_rejected():
    assert qr_auth.rotate("") is None
    assert qr_auth.rotate("garbage") is None
    assert qr_auth.rotate("unknownfam.unknownjti") is None


def test_consume_grant_miss_does_not_rewrite_state():
    # A MISS (unknown grant) must never rewrite/fsync the state file. Before
    # the fix consume_grant wrote on every miss — an unauthenticated,
    # event-loop-blocking fsync DoS amplifier.
    qr_auth.mint_grant()
    path = qr_auth._path()
    before = path.stat().st_mtime_ns
    for _ in range(8):
        assert qr_auth.consume_grant("nonexistent-token") is False
    assert path.stat().st_mtime_ns == before, "consume_grant miss rewrote state"


def test_rotate_unknown_family_does_not_rewrite_state():
    qr_auth.issue_session("alice")
    path = qr_auth._path()
    before = path.stat().st_mtime_ns
    for _ in range(8):
        assert qr_auth.rotate("unknownfam.unknownjti") is None
    assert path.stat().st_mtime_ns == before, "rotate unknown-family rewrote state"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all qr_auth checks passed")
