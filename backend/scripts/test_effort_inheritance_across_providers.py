"""A reasoning_effort inherited from another session must be fitted onto the
target provider instead of rejected.

Locks the Antigravity case: the sender runs at 'medium', the target provider
exposes no efforts at all, and resolving the target's effort must yield "" so
session creation succeeds. Explicitly requested unsupported efforts must still
raise, so a caller asking for something real never gets a silent downgrade.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-effort-inheritance-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

import config_store  # noqa: E402
import main  # noqa: E402
import runtime_profile  # noqa: E402


def main_test() -> int:
    source = config_store.get_default_provider()
    source_record = config_store.get_provider(source["id"]) or {}
    source_efforts = runtime_profile.reasoning_efforts(source_record)
    assert source_efforts, "default provider must expose efforts for this test"
    sender_effort = source_efforts[0]

    effortless = config_store.add_provider({
        "name": "Effortless Test Provider",
        "kind": "agy",
        "mode": source.get("mode") or "subscription",
        "default_model": "effortless-model",
        "custom_models": ["effortless-model"],
    })
    effortless_id = effortless["id"]
    effortless_record = config_store.get_provider(effortless_id) or {}
    assert not runtime_profile.reasoning_efforts(effortless_record), (
        "fixture provider must expose no reasoning efforts"
    )

    # An effort the target cannot honor resolves to "no effort" rather than
    # propagating the sender's value.
    assert runtime_profile.fit_reasoning_effort(
        effortless_record, sender_effort,
    ) == ""
    assert main._inherited_reasoning_effort(effortless_id, sender_effort) == ""

    # A supported inherited effort passes through untouched.
    assert runtime_profile.fit_reasoning_effort(
        source_record, sender_effort,
    ) == sender_effort
    assert main._inherited_reasoning_effort(
        source["id"], sender_effort,
    ) == sender_effort

    # An unsupported value falls back to the provider default when it has one.
    default_effort = str(source_record.get("default_reasoning_effort") or "")
    if default_effort:
        assert runtime_profile.fit_reasoning_effort(
            source_record, "not-a-real-effort",
        ) == default_effort

    # An empty inherited value still lands on something the provider allows.
    assert runtime_profile.fit_reasoning_effort(source_record, "") in source_efforts
    assert runtime_profile.fit_reasoning_effort(effortless_record, "") == ""

    # Explicitly requested unsupported efforts remain a hard error.
    raised = False
    try:
        main._provider_reasoning_effort(effortless_id, sender_effort)
    except HTTPException as exc:
        raised = True
        assert exc.status_code == 400
        assert "does not support reasoning_effort" in str(exc.detail)
    assert raised, "explicit unsupported effort must raise"

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
