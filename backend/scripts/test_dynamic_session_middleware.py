from __future__ import annotations

import os
import sys

import itsdangerous

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth  # noqa: E402
import dynamic_session_middleware as dsm  # noqa: E402


def _set_secret(monkeypatch, secret: str):
    monkeypatch.setattr(auth, "get_session_secret", lambda: secret)


def _make_middleware(monkeypatch, secret: str = "test-secret"):
    _set_secret(monkeypatch, secret)
    # SessionMiddleware stores the ASGI app but never invokes it during
    # construction or signer access, so a bare callable suffices.
    return dsm.DynamicSecretSessionMiddleware(lambda *a, **kw: None)


def test_construction_yields_timestamp_signer(monkeypatch):
    mw = _make_middleware(monkeypatch, "construction-key")
    assert isinstance(mw.signer, itsdangerous.TimestampSigner)


def test_signer_re_derives_key_on_every_access_enabling_rotation(monkeypatch):
    mw = _make_middleware(monkeypatch, "key-one")
    signer1 = mw.signer
    signed_under_key_one = signer1.sign("payload")

    # rotate the keychain secret; the property must pick it up live,
    # without re-constructing the middleware
    _set_secret(monkeypatch, "key-two")
    signer2 = mw.signer

    assert signer1 is not signer2
    # same payload, different keys -> different signatures
    signed_under_key_two = signer2.sign("payload")
    assert signed_under_key_one != signed_under_key_two

    # each signer round-trips its own signature
    assert signer1.unsign(signed_under_key_one) == b"payload"
    assert signer2.unsign(signed_under_key_two) == b"payload"


def test_signer_round_trips_under_current_secret(monkeypatch):
    mw = _make_middleware(monkeypatch, "roundtrip-key")
    signed = mw.signer.sign("hello")
    # reading .signer again returns a fresh signer over the same secret
    assert mw.signer.unsign(signed) == b"hello"


def test_signer_setter_is_a_noop_and_preserves_live_rotation(monkeypatch):
    # SessionMiddleware.__init__ assigns self.signer, routing through the
    # overridden setter; assigning again must not replace the property behavior.
    mw = _make_middleware(monkeypatch, "setter-key")
    mw.signer = "anything"  # type: ignore[assignment]
    _set_secret(monkeypatch, "rotated-after-setter")
    assert mw.signer is not "anything"
    assert isinstance(mw.signer, itsdangerous.TimestampSigner)
