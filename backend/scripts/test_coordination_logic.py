"""Coverage slice for coordination.py error/branch paths.

Drives the real public `lock_ops` API plus the pure helpers against an isolated
BETTER_AGENT_HOME tempdir (real persistence, no module-level mocks). Closes the
fail-closed store-parse branches, the *_required / not_locked / invalid_holder_token
returns across every op, the multi-token holder_tokens_by_key branches, the
acquire timeout-cleanup path, and the normalize_op / lock_ops dispatch table.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _test_home
import pytest

pytestmark = pytest.mark.anyio
_TMP = _test_home.isolate("bc-coordination-logic-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination  # noqa: E402

_FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _FAILURES.append(msg)
        print(f"  FAIL: {msg}")


_OWNER_A = {
    "principal_extension_id": "core",
    "app_session_id": "session-a",
    "cwd": "/repo",
    "provider_id": "pa",
}
_OWNER_B = {
    "principal_extension_id": "core",
    "app_session_id": "session-b",
    "cwd": "/repo",
    "provider_id": "pb",
}


def reset() -> None:
    coordination._clear_for_tests()


def _store_path() -> Path:
    return coordination._lock_store_path()


def _write_store_raw(payload: object) -> None:
    path = _store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Store load / parse fail-closed branches
# ---------------------------------------------------------------------------


async def test_load_fresh_missing_store_initializes_empty() -> None:
    reset()
    _store_path().unlink(missing_ok=True)
    result = await coordination.lock_ops(key="k1", owner=_OWNER_A)
    check(result.get("success") is True, "acquire succeeds against a missing store (fresh-load path)")


async def test_load_corrupt_json_raises() -> None:
    reset()
    _write_store_raw("{not valid json")
    with pytest.raises(RuntimeError):
        await coordination.lock_ops(key="k1", owner=_OWNER_A)


async def test_load_non_dict_root_raises() -> None:
    reset()
    _write_store_raw(json.dumps([1, 2, 3]))
    with pytest.raises(RuntimeError):
        await coordination.lock_ops(key="k1", owner=_OWNER_A)


async def test_load_wrong_schema_version_raises() -> None:
    reset()
    _write_store_raw({"schema_version": 999, "locks": {}})
    with pytest.raises(RuntimeError):
        await coordination.lock_ops(key="k1", owner=_OWNER_A)


async def test_load_invalid_locks_shape_raises() -> None:
    reset()
    _write_store_raw({"schema_version": 1, "locks": "not-a-dict"})
    with pytest.raises(RuntimeError):
        await coordination.lock_ops(key="k1", owner=_OWNER_A)


async def test_load_invalid_record_shape_raises() -> None:
    reset()
    _write_store_raw({"schema_version": 1, "locks": {"k1": "not-a-dict"}})
    with pytest.raises(RuntimeError):
        await coordination.lock_ops(key="k1", owner=_OWNER_A)


async def test_load_skips_empty_record_key() -> None:
    reset()
    _write_store_raw(
        {
            "schema_version": 1,
            "locks": {
                "": {"holder_token": "t"},
                "k1": {"holder_token": "t", "expires_at": 9_999_999_999.0},
            },
        }
    )
    # Empty key is dropped during normalize; store loads cleanly, k1 is visible.
    # _load_locks_unlocked only runs inside an op, so drive one to materialize it.
    await coordination.lock_ops(key="", owned=True, owner=_OWNER_A)
    loaded = coordination._locks
    check("" not in loaded, "empty-string lock key is dropped on load")
    check("k1" in loaded, "non-empty lock key survives load")


async def test_assert_regular_or_missing_rejects_symlink() -> None:
    reset()
    path = _store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    os.symlink("/nonexistent/coordination-target", path)
    try:
        with pytest.raises(RuntimeError):
            await coordination.lock_ops(key="k1", owner=_OWNER_A)
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Pure helpers: _normalize_keys, _clamp_timeout, _clamp_lease, _normalize_owner,
# _same_trusted_owner, _normalize_op
# ---------------------------------------------------------------------------


def test_normalize_keys_dedup_and_empty_skip() -> None:
    single_key, keys = coordination._normalize_keys("k", None)
    check(single_key == "k" and keys == ["k"], "single key normalizes to itself")
    _, keys = coordination._normalize_keys("", ["a", "", "a", "  b  ", "b"])
    check(keys == ["a", "b"], "keys list strips empties, trims, and dedups in order")


def test_clamp_timeout_contract() -> None:
    check(coordination._clamp_timeout(None) == coordination._DEFAULT_MULTI_LOCK_TIMEOUT_SECONDS, "None timeout -> default")
    check(coordination._clamp_timeout(0) == 0, "zero timeout -> zero")
    check(coordination._clamp_timeout(-5) == 0, "negative timeout -> zero")
    check(coordination._clamp_timeout(9999) == coordination._MAX_MULTI_LOCK_TIMEOUT_SECONDS, "oversized timeout clamped to max")
    check(coordination._clamp_timeout("nope") is None, "non-numeric timeout -> None")
    check(coordination._clamp_timeout(7) == 7, "in-range timeout passes through")


def test_clamp_lease_contract() -> None:
    check(coordination._clamp_lease(None) == float(coordination._DEFAULT_LOCK_LEASE_SECONDS), "None lease -> default")
    check(coordination._clamp_lease(-1) is None, "non-positive lease -> None")
    check(coordination._clamp_lease(0) is None, "zero lease -> None")
    check(coordination._clamp_lease("bad") is None, "non-numeric lease -> None")
    check(coordination._clamp_lease(1) == coordination._MIN_LOCK_LEASE_SECONDS, "undersized lease clamped to min")
    check(coordination._clamp_lease(9999) == float(coordination._MAX_LOCK_LEASE_SECONDS), "oversized lease clamped to max")
    check(coordination._clamp_lease(60) == 60, "in-range lease passes through")


def test_normalize_owner_strips_empty_fields() -> None:
    normalized = coordination._normalize_owner({"cwd": "   ", "pid": "5", "app_session_id": None})
    check(normalized == {"pid": "5"}, "owner normalizer drops whitespace-only and None fields, keeps populated ones")


def test_same_trusted_owner_provider_mismatch() -> None:
    rec = {"owner": dict(_OWNER_A)}
    check(coordination._same_trusted_owner(rec, _OWNER_A) is True, "identical owner matches")
    mismatched_provider = dict(_OWNER_A)
    mismatched_provider["provider_id"] = "pdifferent"
    check(coordination._same_trusted_owner(rec, mismatched_provider) is False, "provider mismatch breaks trusted-owner match")
    check(coordination._same_trusted_owner(rec, _OWNER_B) is False, "different app_session breaks trusted-owner match")


def test_normalize_op_flag_dispatch() -> None:
    check(coordination._normalize_op(op="", release=True, renew=False, validate=False, reattach=False, owned=True) == "release_owned", "release+owned flags -> release_owned")
    check(coordination._normalize_op(op="", release=True, renew=False, validate=False, reattach=False, owned=False) == "release", "release flag -> release")
    check(coordination._normalize_op(op="", release=False, renew=True, validate=False, reattach=False, owned=False) == "renew", "renew flag -> renew")
    check(coordination._normalize_op(op="", release=False, renew=False, validate=True, reattach=False, owned=False) == "validate", "validate flag -> validate")
    check(coordination._normalize_op(op="", release=False, renew=False, validate=False, reattach=True, owned=False) == "reattach", "reattach flag -> reattach")
    check(coordination._normalize_op(op="", release=False, renew=False, validate=False, reattach=False, owned=True) == "list_owned", "owned flag -> list_owned")
    check(coordination._normalize_op(op="", release=False, renew=False, validate=False, reattach=False, owned=False) == "acquire", "no flags -> acquire")
    check(coordination._normalize_op(op="  Release-Owned ", release=False, renew=False, validate=False, reattach=False, owned=False) == "release_owned", "explicit op string lowercased and dash-normalized")
    check(coordination._normalize_op(op="bogus", release=False, renew=False, validate=False, reattach=False, owned=False) == "bogus", "unknown explicit op passes through for later invalid_op rejection")


# ---------------------------------------------------------------------------
# release / validate error returns
# ---------------------------------------------------------------------------


async def test_release_requires_holder_token() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", release=True, holder_token="")
    check(res == {"success": False, "error": "holder_token_required"}, "release with empty token -> holder_token_required")


async def test_release_not_locked() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", release=True, holder_token="t")
    check(res.get("error") == "not_locked", "release of a key nobody holds -> not_locked")


async def test_release_invalid_holder_token() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    res = await coordination.lock_ops(key="k1", release=True, holder_token="wrong-token")
    check(res.get("error") == "invalid_holder_token", "release with wrong token -> invalid_holder_token")


async def test_validate_requires_holder_token() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", validate=True, holder_token="")
    check(res == {"success": False, "error": "holder_token_required"}, "validate with empty token -> holder_token_required")


async def test_validate_not_locked() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", validate=True, holder_token="t")
    check(res.get("error") == "not_locked", "validate of a key nobody holds -> not_locked")


async def test_validate_invalid_holder_token() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    res = await coordination.lock_ops(key="k1", validate=True, holder_token="wrong-token")
    check(res.get("error") == "invalid_holder_token", "validate with wrong token -> invalid_holder_token")


# ---------------------------------------------------------------------------
# release_owned / list_owned
# ---------------------------------------------------------------------------


async def test_release_owned_requires_owner() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", release=True, owned=True, owner=None)
    check(res.get("error") == "owner_required", "release_owned with no owner -> owner_required")


async def test_release_owned_blocked_by_other_owner() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    res = await coordination.lock_ops(key="", keys=["k1"], release=True, owned=True, owner=_OWNER_B)
    check(res.get("error") == "not_lock_owner", "release_owned of another owner's lock -> not_lock_owner")
    check(res.get("blocked_keys") == ["k1"], "release_owned blocked reports blocked_keys")


async def test_release_owned_skips_missing_and_releases_self() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    # k-missing is absent (continue branch), k1 is self-owned and gets released.
    res = await coordination.lock_ops(key="", keys=["k-missing", "k1"], release=True, owned=True, owner=_OWNER_A)
    check(res.get("success") is True, "release_owned succeeds when only self-owned keys are present")
    check(sorted(res.get("keys") or []) == ["k1"], "release_owned reports only the keys actually released")


async def test_list_owned_requires_owner() -> None:
    reset()
    res = await coordination.lock_ops(key="", owned=True, owner=None)
    check(res.get("error") == "owner_required", "list_owned with no owner -> owner_required")


async def test_list_owned_returns_only_self_locks() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    await coordination.lock_ops(key="k2", owner=_OWNER_B)
    res = await coordination.lock_ops(key="", owned=True, owner=_OWNER_A)
    check(res.get("success") is True, "list_owned succeeds")
    check([item["key"] for item in res.get("locks") or []] == ["k1"], "list_owned returns only this owner's locks")


# ---------------------------------------------------------------------------
# reattach: owner_required, not_locked, multi-token holder_tokens_by_key
# ---------------------------------------------------------------------------


async def test_reattach_requires_owner() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", reattach=True, owner=None)
    check(res.get("error") == "owner_required", "reattach with no owner -> owner_required")


async def test_reattach_not_locked() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", reattach=True, owner=_OWNER_A)
    check(res.get("error") == "not_locked", "reattach of a key nobody holds -> not_locked")


async def test_reattach_multi_token_returns_tokens_by_key() -> None:
    reset()
    # Two independent single-key acquires by the same trusted owner produce
    # distinct holder tokens, exercising the holder_tokens_by_key branch.
    a = await coordination.lock_ops(key="ka", owner=_OWNER_A)
    b = await coordination.lock_ops(key="kb", owner=_OWNER_A)
    res = await coordination.lock_ops(key="", keys=["ka", "kb"], reattach=True, owner=_OWNER_A)
    check(res.get("success") is True, "reattach succeeds for same-owner multi-key")
    tokens_by_key = res.get("holder_tokens_by_key") or {}
    check(set(tokens_by_key) == {"ka", "kb"}, "reattach multi-token reports tokens for each key")
    check(
        tokens_by_key.get("ka") == a.get("holder_token") and tokens_by_key.get("kb") == b.get("holder_token"),
        "reattach multi-token returns the actual per-key holder tokens",
    )


# ---------------------------------------------------------------------------
# renew: not_locked, blocked (not owner), multi-token holder_tokens_by_key
# ---------------------------------------------------------------------------


async def test_renew_not_locked() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", renew=True, owner=_OWNER_A)
    check(res.get("error") == "not_locked", "renew of a key nobody holds -> not_locked")


async def test_renew_blocked_when_not_owner_and_no_token() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    res = await coordination.lock_ops(key="k1", renew=True, holder_token="", owner=_OWNER_B)
    check(res.get("error") == "not_lock_owner", "renew with no token and wrong owner -> not_lock_owner")
    check(res.get("blocked_keys") == ["k1"], "renew blocked reports blocked_keys")


async def test_renew_multi_token_returns_tokens_by_key() -> None:
    reset()
    a = await coordination.lock_ops(key="ka", owner=_OWNER_A)
    b = await coordination.lock_ops(key="kb", owner=_OWNER_A)
    res = await coordination.lock_ops(key="", keys=["ka", "kb"], renew=True, holder_token="", owner=_OWNER_A, lease_seconds=60)
    check(res.get("success") is True, "renew succeeds for same-owner multi-key via owner match")
    tokens_by_key = res.get("holder_tokens_by_key") or {}
    check(
        tokens_by_key.get("ka") == a.get("holder_token") and tokens_by_key.get("kb") == b.get("holder_token"),
        "renew multi-token returns the actual per-key holder tokens",
    )


# ---------------------------------------------------------------------------
# acquire: multi-blocked (blocked-payload computed once) + timeout cleanup
# ---------------------------------------------------------------------------


async def test_acquire_multi_blocked_reports_all_blocked_keys() -> None:
    reset()
    await coordination.lock_ops(key="ka", owner=_OWNER_A)
    await coordination.lock_ops(key="kb", owner=_OWNER_A)
    res = await coordination.lock_ops(key="", keys=["ka", "kb"], owner=_OWNER_B, timeout_seconds=0)
    check(res.get("error") == "timeout", "acquire blocked on every key until deadline -> timeout")
    check(set(res.get("blocked_keys") or []) == {"ka", "kb"}, "acquire reports every key it was blocked on")


async def test_acquire_timeout_releases_partially_acquired_keys() -> None:
    reset()
    # ka is held by another owner (blocks); kb is free (acquired by this call's
    # token). At the deadline the cleanup loop pops the just-acquired kb.
    await coordination.lock_ops(key="ka", owner=_OWNER_A)
    res = await coordination.lock_ops(key="", keys=["ka", "kb"], owner=_OWNER_B, timeout_seconds=0)
    check(res.get("error") == "timeout", "partial acquisition hits timeout")
    check(res.get("key") == "ka", "timeout reports the blocking key")
    # kb was acquired then rolled back on timeout.
    kb_after = coordination._locks.get("kb")
    check(kb_after is None, "acquire timeout cleanup releases partially acquired keys")


# ---------------------------------------------------------------------------
# lock_ops dispatch table
# ---------------------------------------------------------------------------


async def test_lock_ops_key_required() -> None:
    reset()
    res = await coordination.lock_ops(key="", owner=_OWNER_A)
    check(res == {"success": False, "error": "key_required"}, "acquire with no key -> key_required")


async def test_lock_ops_invalid_op() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", op="bogus", owner=_OWNER_A)
    check(res == {"success": False, "error": "invalid_op"}, "unknown explicit op -> invalid_op")


async def test_lock_ops_invalid_lease_seconds() -> None:
    reset()
    res = await coordination.lock_ops(key="k1", lease_seconds=-1, owner=_OWNER_A)
    check(res == {"success": False, "error": "invalid_lease_seconds"}, "non-positive lease -> invalid_lease_seconds")


async def test_lock_ops_invalid_timeout_seconds() -> None:
    reset()
    res = await coordination.lock_ops(key="", keys=["ka", "kb"], timeout_seconds="bad", owner=_OWNER_A)
    check(res == {"success": False, "error": "invalid_timeout_seconds"}, "non-numeric multi-key timeout -> invalid_timeout_seconds")


async def test_lock_ops_release_owned_dispatch_by_flag() -> None:
    reset()
    await coordination.lock_ops(key="k1", owner=_OWNER_A)
    res = await coordination.lock_ops(key="k1", release=True, owned=True, owner=_OWNER_A)
    check(res.get("success") is True, "release=True&owned=True dispatches to release_owned and succeeds for self")


# ---------------------------------------------------------------------------
# standalone runner
# ---------------------------------------------------------------------------


async def _run_all() -> None:
    import inspect

    funcs = [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    for fn in funcs:
        coordination._clear_for_tests()
        print(f"-> {fn.__name__}")
        result = fn()
        if inspect.isawaitable(result):
            await result
        coordination._clear_for_tests()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURE(S):")
        for msg in _FAILURES:
            print(f"  - {msg}")
        raise SystemExit(1)
    print(f"\nAll {len(funcs)} coordination-logic checks passed.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_all())
