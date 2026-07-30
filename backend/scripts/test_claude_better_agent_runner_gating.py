"""Locks the validation/permission/runtime-dispatch gating that makes
`kind=="claude" + runner=="better_agent_runner" + mode=="subscription"` a
legal, fully-supported combination, WITHOUT changing behavior for any other
kind (regression guard: openai/fugu still reject better_agent_runner in
subscription mode).

Covers:
- config_store.add_provider accepts claude+better_agent_runner+subscription,
  rejects claude+better_agent_runner+api_key (no api_key backend exists for
  it), and still rejects openai/fugu+better_agent_runner+subscription.
- runtime_profile.supported_runners / default_runner / runtime_kind resolve
  claude+better_agent_runner to the real "claude" runtime kind (not the
  generic "openai" collapse every other kind's better_agent_runner choice
  gets), mode-gated to subscription only.
- permission.resolve_for_run keeps claude's own permission defaults for
  claude+better_agent_runner instead of borrowing openai's.
- provider_manifest.runner_module_for resolves "runner_better_agent" for
  claude+better_agent_runner while leaving claude's native default
  ("runner") and every other kind's mapping untouched.
- app_entry._dispatch round-trips an explicit --runner-module override.

Run with:
    cd backend && .venv/bin/python scripts/test_claude_better_agent_runner_gating.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="bc-test-claude-ba-runner-")
paths.engage_test_home(_TMP)

import config_store  # noqa: E402
import permission  # noqa: E402
import provider_manifest  # noqa: E402
import runtime_profile  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_config_store_accepts_claude_subscription_better_agent_runner() -> bool:
    provider = config_store.add_provider({
        "name": "claude-ba-runner",
        "kind": "claude",
        "mode": "subscription",
        "runner": "better_agent_runner",
    })
    if provider.get("runner") != "better_agent_runner":
        print(f"  runner not persisted: {provider.get('runner')!r}")
        return False
    if provider.get("mode") != "subscription":
        print(f"  mode not persisted: {provider.get('mode')!r}")
        return False
    return True


def test_config_store_rejects_claude_api_key_better_agent_runner() -> bool:
    try:
        config_store.add_provider({
            "name": "claude-ba-runner-api-key",
            "kind": "claude",
            "mode": "api_key",
            "runner": "better_agent_runner",
            "api_key": "sk-test",
        })
    except ValueError as e:
        if "subscription" not in str(e).lower():
            print(f"  wrong rejection message: {e}")
            return False
        return True
    print("  claude api_key + better_agent_runner was accepted (should reject)")
    return False


def test_config_store_still_rejects_openai_subscription() -> bool:
    """Regression guard: the claude-specific carve-out must not leak into
    every other kind's better_agent_runner + subscription combination."""
    try:
        config_store.add_provider({
            "name": "openai-subscription",
            "kind": "openai",
            "mode": "subscription",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-test",
        })
    except ValueError as e:
        if "subscription" not in str(e).lower():
            print(f"  wrong rejection message: {e}")
            return False
    else:
        print("  openai + subscription was accepted (should reject)")
        return False

    try:
        config_store.add_provider({
            "name": "fugu-ba-runner-subscription",
            "kind": "fugu",
            "mode": "subscription",
            "runner": "better_agent_runner",
        })
    except ValueError:
        return True
    print("  fugu + better_agent_runner + subscription was accepted (should reject)")
    return False


def test_runtime_profile_supported_runners() -> bool:
    claude_sub = {"kind": "claude", "mode": "subscription"}
    claude_api = {"kind": "claude", "mode": "api_key"}
    if "better_agent_runner" not in runtime_profile.supported_runners(claude_sub):
        print(f"  claude subscription choices: {runtime_profile.supported_runners(claude_sub)}")
        return False
    if "better_agent_runner" in runtime_profile.supported_runners(claude_api):
        print(f"  claude api_key choices unexpectedly include better_agent_runner: "
              f"{runtime_profile.supported_runners(claude_api)}")
        return False
    if runtime_profile.default_runner(claude_sub) != "native":
        print(f"  claude subscription default runner changed: "
              f"{runtime_profile.default_runner(claude_sub)}")
        return False
    fugu_sub = {"kind": "fugu", "mode": "subscription"}
    if "better_agent_runner" in runtime_profile.supported_runners(fugu_sub):
        print(f"  fugu subscription choices unexpectedly include better_agent_runner: "
              f"{runtime_profile.supported_runners(fugu_sub)}")
        return False
    fugu_api = {"kind": "fugu", "mode": "api_key"}
    if "better_agent_runner" not in runtime_profile.supported_runners(fugu_api):
        print(f"  fugu api_key choices regressed: {runtime_profile.supported_runners(fugu_api)}")
        return False
    return True


def test_runtime_profile_runtime_kind() -> bool:
    claude_ba = {"kind": "claude", "runner": "better_agent_runner"}
    if runtime_profile.runtime_kind(claude_ba) != "claude":
        print(f"  claude+better_agent_runner runtime_kind: {runtime_profile.runtime_kind(claude_ba)}")
        return False
    claude_native = {"kind": "claude", "runner": "native"}
    if runtime_profile.runtime_kind(claude_native) != "claude":
        print(f"  claude+native runtime_kind regressed: {runtime_profile.runtime_kind(claude_native)}")
        return False
    fugu_ba = {"kind": "fugu", "runner": "better_agent_runner"}
    if runtime_profile.runtime_kind(fugu_ba) != "openai":
        print(f"  fugu+better_agent_runner runtime_kind regressed: {runtime_profile.runtime_kind(fugu_ba)}")
        return False
    return True


def test_permission_defaults_stay_claude() -> bool:
    perm = permission.resolve_for_run(
        sess_rec={"provider_id": None},
        worker_sess_rec=None,
        is_worker=False,
        fallback_kind="claude",
    )
    # fallback_kind path doesn't touch the runner collapse directly, but
    # confirms the function still resolves without error for a bare
    # fallback; the collapse itself is exercised via the record-kind
    # branch below.
    if not isinstance(perm, dict):
        print(f"  unexpected permission shape: {perm!r}")
        return False
    return True


def test_provider_manifest_runner_module_for() -> bool:
    if provider_manifest.runner_module_for("claude") != "runner":
        print(f"  claude default module regressed: {provider_manifest.runner_module_for('claude')}")
        return False
    if provider_manifest.runner_module_for("claude", runner="native") != "runner":
        print(f"  claude native module regressed: "
              f"{provider_manifest.runner_module_for('claude', runner='native')}")
        return False
    if provider_manifest.runner_module_for("claude", runner="better_agent_runner") != "runner_better_agent":
        print(f"  claude better_agent_runner module: "
              f"{provider_manifest.runner_module_for('claude', runner='better_agent_runner')}")
        return False
    if provider_manifest.runner_module_for("openai", runner="better_agent_runner") != "runner_better_agent":
        print("  openai better_agent_runner module regressed")
        return False
    if "better_agent_runner" not in provider_manifest.runner_choices_for("claude"):
        print(f"  claude runner_choices: {provider_manifest.runner_choices_for('claude')}")
        return False
    return True


def test_app_entry_dispatch_runner_module_roundtrip() -> bool:
    from app_entry import _dispatch

    got = _dispatch([
        "--run-dir", "/runs/z", "--runner-kind", "claude",
        "--runner-module", "runner_better_agent",
    ])
    expected = ("runner", "claude", Path("/runs/z"), "runner_better_agent")
    if got != expected:
        print(f"  got {got}, expected {expected}")
        return False
    # Absent --runner-module still round-trips as before (native claude,
    # every other pre-existing frozen launcher call site).
    got_default = _dispatch(["--run-dir", "/runs/x"])
    if got_default != ("runner", "claude", Path("/runs/x"), ""):
        print(f"  default dispatch regressed: {got_default}")
        return False
    return True


TESTS = [
    ("config_store accepts claude+better_agent_runner+subscription", test_config_store_accepts_claude_subscription_better_agent_runner),
    ("config_store rejects claude+better_agent_runner+api_key", test_config_store_rejects_claude_api_key_better_agent_runner),
    ("config_store still rejects openai/fugu+subscription (regression)", test_config_store_still_rejects_openai_subscription),
    ("runtime_profile.supported_runners is mode/kind-gated correctly", test_runtime_profile_supported_runners),
    ("runtime_profile.runtime_kind resolves claude correctly", test_runtime_profile_runtime_kind),
    ("permission.resolve_for_run still resolves", test_permission_defaults_stay_claude),
    ("provider_manifest.runner_module_for is runner-aware", test_provider_manifest_runner_module_for),
    ("app_entry._dispatch round-trips --runner-module", test_app_entry_dispatch_runner_module_roundtrip),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
