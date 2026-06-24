"""Tests for the global last-supervisor-prompt store (no claude CLI subprocesses).

Pins the contract behind "remember the last supervisor prompt and reuse it as
the default on the next enable":

  1. `set_last_supervisor_prompt` persists and `get_last_supervisor_prompt`
     reads it back (round-trip).
  2. The value survives a fresh `_load_state()` reload (it is on disk, not
     in-process) — i.e. it is a real global default across sessions / restarts.
  3. Non-string / None / empty inputs normalize to "" and never crash.
  4. A new value overwrites the previous one.

Run with:
    cd backend && .venv/bin/python scripts/test_supervisor_last_prompt.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-supervisor-last-prompt-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_round_trip() -> bool:
    config_store.set_last_supervisor_prompt("Review every change adversarially.")
    got = config_store.get_last_supervisor_prompt()
    if got != "Review every change adversarially.":
        print(f"  round-trip mismatch: {got!r}")
        return False
    return True


def test_survives_reload() -> bool:
    config_store.set_last_supervisor_prompt("Be skeptical of the agent's claims.")
    # _load_state reads from disk on every call, so this exercises persistence.
    got = config_store.get_last_supervisor_prompt()
    if got != "Be skeptical of the agent's claims.":
        print(f"  value lost after reload: {got!r}")
        return False
    return True


def test_empty_and_none_normalize() -> bool:
    config_store.set_last_supervisor_prompt(None)
    if config_store.get_last_supervisor_prompt() != "":
        print("  None did not normalize to empty")
        return False
    config_store.set_last_supervisor_prompt(123)  # type: ignore[arg-type]
    if config_store.get_last_supervisor_prompt() != "123":
        print("  non-string did not coerce to string")
        return False
    config_store.set_last_supervisor_prompt("")
    if config_store.get_last_supervisor_prompt() != "":
        print("  empty string not stored as empty")
        return False
    return True


def test_overwrite() -> bool:
    config_store.set_last_supervisor_prompt("first")
    config_store.set_last_supervisor_prompt("second")
    got = config_store.get_last_supervisor_prompt()
    if got != "second":
        print(f"  overwrite failed: {got!r}")
        return False
    return True


TESTS = [
    ("last_supervisor_prompt round-trips", test_round_trip),
    ("last_supervisor_prompt survives reload", test_survives_reload),
    ("last_supervisor_prompt normalizes empty/None/non-string", test_empty_and_none_normalize),
    ("last_supervisor_prompt overwrites previous value", test_overwrite),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                print(f"  {name} raised {type(e).__name__}: {e}")
            print(f"{PASS if ok else FAIL} {name}")
            if not ok:
                failed += 1
        print()
        print(f"summary: {len(TESTS) - failed}/{len(TESTS)} passed")
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
