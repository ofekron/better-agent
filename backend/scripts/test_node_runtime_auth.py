from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_runtime_auth as nra  # noqa: E402


def test_token_is_stable_nonempty_string():
    first = nra.token()
    assert isinstance(first, str)
    assert first
    # module-level token does not regenerate per call
    assert nra.token() == first


def test_verify_accepts_real_token():
    assert nra.verify(nra.token()) is True


def test_verify_rejects_empty_string():
    # short-circuit branch: falsy candidate never reaches compare_digest
    assert nra.verify("") is False


def test_verify_rejects_none():
    assert nra.verify(None) is False


def test_verify_rejects_wrong_token():
    assert nra.verify("definitely-not-the-real-token") is False


def test_verify_rejects_partial_token():
    # a prefix of the real token must not be accepted (constant-time compare)
    real = nra.token()
    assert nra.verify(real[: len(real) // 2]) is False
