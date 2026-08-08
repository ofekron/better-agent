#!/usr/bin/env python3
"""Every installable provider kind must seed a provider record + runtime
profile that survives strict load-time canonicalization.

Regression origin: seeding hardcoded the global DEFAULT_REASONING_EFFORT
("medium") for every kind, so kinds whose reasoning-effort options exclude
"medium" (agy, amp, copilot, qwen — no options at all; opencode —
minimal/high/max) produced a noncanonical record and crashed first install
with "unsupported provider config schema: noncanonical provider record".
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import paths  # noqa: E402

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-seed-canonical-"))
paths.engage_test_home(str(TEST_HOME))

import config_store  # noqa: E402
import provider_manifest  # noqa: E402
import runtime_profile  # noqa: E402


def _seed_state_with_every_installable_kind() -> dict:
    state = config_store._seed_default_state()
    seeded_kinds = {p.get("kind") for p in state["providers"]}
    for kind in provider_manifest.installable_kinds():
        if kind in seeded_kinds:
            continue
        provider = config_store._new_provider_record(kind)
        state["providers"].append(provider)
        state["runtime_profiles"].append(
            config_store._seed_profile_for_provider(provider)
        )
    return state


def test_seeded_effort_is_valid_for_every_kind() -> None:
    for kind in provider_manifest.installable_kinds():
        provider = config_store._new_provider_record(kind)
        profile = config_store._seed_profile_for_provider(provider)
        record = runtime_profile.provider_record_for_runner(
            provider, profile["runner"]
        )
        options = config_store.reasoning_effort_options_for_provider(record)
        effort = profile["default_reasoning_effort"]
        expected = effort in options if options else effort == ""
        assert expected, f"{kind}: seeded effort {effort!r} not in {options!r}"


def test_seeded_state_round_trips_through_load() -> None:
    config_store._save_state(_seed_state_with_every_installable_kind())
    with config_store._state_cache_lock:
        config_store._state_cache = None
    state = config_store._load_state()
    kinds = {p["kind"] for p in state["providers"]}
    missing = set(provider_manifest.installable_kinds()) - kinds
    assert not missing, f"kinds dropped by load: {sorted(missing)}"


def main() -> int:
    try:
        test_seeded_effort_is_valid_for_every_kind()
        test_seeded_state_round_trips_through_load()
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
