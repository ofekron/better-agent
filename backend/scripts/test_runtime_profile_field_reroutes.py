#!/usr/bin/env python3
"""Regression locks for the v3 field-move reroutes.

The config schema v3 moved runner/default_model/default_reasoning_effort off
provider records onto runtime profiles. This suite locks the reroutes the
adversarial review found missing: a source scan that fails when a new reader
of the dropped fields appears on a provider-record path, plus behavioral
checks for the profile-derived fallbacks."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-field-reroutes-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
os.environ["BETTER_CLAUDE_HOME"] = str(TEST_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import config_store  # noqa: E402
import runtime_profile  # noqa: E402


def test_no_new_dropped_field_readers() -> None:
    """Source scan: provider-record reads of the dropped fields are only
    allowed at the explicitly reviewed sites (migrations, injected-view
    plumbing, profile entities). A new hit means an unrerouted reader."""
    allowed = {
        # (file, pattern) pairs that legitimately touch the dropped names:
        # migrations / legacy-shape handling in config_store, injected-view
        # producers, and the runtime-profile entity itself.
        "config_store.py",
        "runtime_profile.py",  # injected-view helpers + lazy profile fallback
        "harness_profile_store.py",  # harness pin entity fields (own schema)
        "harness_profile_resolver.py",
        "harness_fields.py",
        "harness_profiles_api.py",
        "provider_kimi.py",  # reads the Kimi CLI's own TOML, not our record
        "providers_api.py",  # pydantic payloads (ignored server-side)
        # runtime_record() readers: the execution snapshot is the INJECTED
        # runtime view (hydrate_provider_execution folds profile defaults in),
        # so these reads are profile-derived by construction.
        "provider_claude.py",
        "provider_openai.py",
    }
    pattern = re.compile(
        r"""(?:provider|record|rec|selected_provider)\s*(?:or\s*\{\})?\s*\)?\.get\(\s*['"](?:default_model|default_reasoning_effort)['"]"""
    )
    offenders: list[str] = []
    for path in sorted(BACKEND.glob("*.py")):
        if path.name in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "unrerouted provider-record readers of dropped fields:\n"
        + "\n".join(offenders)
    )


def test_runtime_kind_follows_profile_runner() -> None:
    """A store-shape record (no runner) resolves its runtime kind through the
    preferred live profile — a better_agent_runner profile flips a non-claude
    kind to the openai runtime."""
    provider = config_store.add_provider({
        "name": "Fugu Kind", "kind": "fugu", "mode": "api_key",
    })
    stored = next(
        p
        for p in config_store._load_state()["providers"]
        if p["id"] == provider["id"]
    )
    assert "runner" not in stored
    seeded = config_store.provider_execution_defaults(provider["id"])
    assert seeded["runtime_profile_id"]
    baseline_kind = config_store._runtime_kind_for_provider(stored)
    if seeded["runner"] != "better_agent_runner":
        ba_profile = config_store.find_live_runtime_profile(
            provider["id"], "better_agent_runner"
        ) or config_store.add_runtime_profile(
            {"provider_id": provider["id"], "runner": "better_agent_runner"}
        )
        deleted, reason = config_store.delete_runtime_profile(
            seeded["runtime_profile_id"]
        )
        assert deleted, reason
        stored = next(
            p
            for p in config_store._load_state()["providers"]
            if p["id"] == provider["id"]
        )
    assert config_store._runtime_kind_for_provider(stored) == "openai", (
        "runtime kind must follow the preferred live profile's runner"
    )
    assert baseline_kind in ("fugu", "openai")


def test_fit_reasoning_effort_uses_profile_default() -> None:
    """Cross-provider effort inheritance falls back to the pair profile's
    default effort for store-shape records (not straight to options[0])."""
    provider = config_store.add_provider({
        "name": "Effort Kind", "kind": "claude", "mode": "subscription",
    })
    defaults = config_store.provider_execution_defaults(provider["id"])
    record = config_store.get_provider(provider["id"]) or {}
    options = runtime_profile.reasoning_efforts(record)
    if len(options) < 2:
        print("  (provider exposes <2 efforts; fallback ordering not observable)")
        return
    profile_default = options[1]
    assert config_store.update_runtime_profile(
        defaults["runtime_profile_id"], {"default_reasoning_effort": profile_default}
    )
    stored = next(
        p
        for p in config_store._load_state()["providers"]
        if p["id"] == provider["id"]
    )
    fitted = runtime_profile.fit_reasoning_effort(
        {**stored, "reasoning_effort_options": list(options)},
        "not-an-option",
        defaults["runner"],
    )
    assert fitted == profile_default, (
        f"expected the pair profile's default {profile_default!r}, got {fitted!r}"
    )


def test_ensure_profiles_is_provider_scoped_and_never_resurrects() -> None:
    provider = config_store.add_provider({
        "name": "Ensure Kind", "kind": "codex", "mode": "subscription",
    })
    state = config_store._load_state()
    seeded = [
        p
        for p in state["runtime_profiles"]
        if p["provider_id"] == provider["id"]
    ]
    assert len(seeded) == 1

    # Live profile present → no-op.
    config_store._ensure_runtime_profiles_for_providers(state)
    assert (
        len(
            [
                p
                for p in state["runtime_profiles"]
                if p["provider_id"] == provider["id"]
            ]
        )
        == 1
    )

    # Tombstone-only history → never resurrected, never re-seeded.
    for profile in state["runtime_profiles"]:
        if profile["provider_id"] == provider["id"]:
            profile["deleted_at"] = "2026-01-01T00:00:00+00:00"
    config_store._ensure_runtime_profiles_for_providers(state)
    remaining = [
        p
        for p in state["runtime_profiles"]
        if p["provider_id"] == provider["id"]
    ]
    assert len(remaining) == 1 and remaining[0]["deleted_at"], (
        "tombstone-only history must stay exactly as the user left it"
    )

    # Zero history → seeds exactly one starter profile.
    state["runtime_profiles"] = [
        p
        for p in state["runtime_profiles"]
        if p["provider_id"] != provider["id"]
    ]
    config_store._ensure_runtime_profiles_for_providers(state)
    fresh = [
        p
        for p in state["runtime_profiles"]
        if p["provider_id"] == provider["id"]
    ]
    assert len(fresh) == 1 and not fresh[0]["deleted_at"]


def main() -> int:
    tests = [
        test_no_new_dropped_field_readers,
        test_runtime_kind_follows_profile_runner,
        test_fit_reasoning_effort_uses_profile_default,
        test_ensure_profiles_is_provider_scoped_and_never_resurrects,
    ]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
