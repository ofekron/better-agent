#!/usr/bin/env python3
"""Locks `AttestOnceCache.resolve` on tuple-shaped `(ok, reason)` results.

`FamilyLaunchAttestation.attest_with_reason` funnels tuple-returning
checks through the same cache the bool-returning `RunnerLaunch.attest`
uses. A bool-only cache collapsed those tuples to `True`/`False`, so
callers unpacking the result raised
`TypeError: 'bool' object is not subscriptable`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_execution_common import AttestOnceCache  # noqa: E402


def test_tuple_result_is_returned_verbatim():
    cache = AttestOnceCache()
    ok, reason = cache.resolve(lambda: (True, "ok"), lambda: (True, "meta"))
    assert (ok, reason) == (True, "ok")
    assert cache.resolve(lambda: (True, "ok"), lambda: (True, "meta")) == (True, "meta")


def test_failed_tuple_result_does_not_verify_the_cache():
    cache = AttestOnceCache()
    assert cache.resolve(lambda: (False, "boom"), lambda: (True, "meta")) == (False, "boom")
    assert cache.resolve(lambda: (True, "ok"), lambda: (True, "meta")) == (True, "ok")


def test_bool_result_contract_is_unchanged():
    cache = AttestOnceCache()
    assert cache.resolve(lambda: False, lambda: True) is False
    assert cache.resolve(lambda: True, lambda: False) is True
    assert cache.resolve(lambda: True, lambda: False) is False


if __name__ == "__main__":
    test_tuple_result_is_returned_verbatim()
    test_failed_tuple_result_does_not_verify_the_cache()
    test_bool_result_contract_is_unchanged()
    print("PASS")
