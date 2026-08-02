from __future__ import annotations

import os
import sys
from typing import Callable

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-anthropic-model-catalog-")

import config_store  # noqa: E402
import models  # noqa: E402


def _check_subscription_cold_start_uses_current_anthropic_models() -> bool:
    provider = {
        "id": "anthropic-subscription",
        "kind": "claude",
        "mode": "subscription",
        "custom_models": [],
        "default_model": "",
    }
    actual, retired, has_cache, cached = models._read_catalog_models(provider)
    expected = [
        "best",
        "fable",
        "opus",
        "opus[1m]",
        "sonnet",
        "sonnet[1m]",
        "haiku",
        "claude-fable-5",
        "claude-opus-5[1m]",
        "claude-opus-5",
        "claude-opus-4-8[1m]",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ]
    if actual != expected:
        print(f"  catalog mismatch: {actual!r}")
        return False
    if retired or has_cache or cached is not None:
        print(f"  unexpected cache state: retired={retired!r} has_cache={has_cache!r}")
        return False
    return True


def _check_default_subscription_model_uses_claude_code_alias() -> bool:
    if config_store._default_model_for("subscription", "") != "opus":
        print("  migration default did not use opus alias")
        return False
    state = config_store._seed_default_state()
    claude = next(p for p in state["providers"] if p["kind"] == "claude")
    profile = next(
        p for p in state["runtime_profiles"]
        if p["provider_id"] == claude["id"] and p["runner"] == "native"
    )
    if profile["default_model"] != "opus":
        print(f"  seed default mismatch: {profile['default_model']!r}")
        return False
    return True


TESTS = [
    ("subscription_cold_start_uses_current_anthropic_models", _check_subscription_cold_start_uses_current_anthropic_models),
    ("default_subscription_model_uses_claude_code_alias", _check_default_subscription_model_uses_claude_code_alias),
]


@pytest.mark.parametrize(("name", "check"), TESTS, ids=[name for name, _ in TESTS])
def test_anthropic_model_catalog_contract(
    name: str,
    check: Callable[[], bool],
) -> None:
    assert check(), name


def main_run() -> int:
    failed = False
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"  exception in {name}: {exc}")
        print(("PASS" if ok else "FAIL"), name)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_run())
