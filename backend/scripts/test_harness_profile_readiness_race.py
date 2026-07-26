"""resolve_for_session reads extension readiness TWICE (once inside
compute_default_profile's synthesis gate, once per selected instance while
building the run snapshot). A readiness flip between the two reads must be
handled by DURABILITY, not uniformly:

  * DURABLE (record gone, or `enabled` flipped False -- e.g. auto-quarantine):
    a retry cannot restore the extension, and the next synthesis excludes it
    anyway, so the instance is DROPPED and surfaced. Failing the turn would
    fail a turn that would run identically degraded on retry.

  * RECOVERABLE (still `enabled`, but transiently not runtime-ready -- e.g. an
    internal-LLM assignment being reassigned): restores itself with no user
    action, so the resolve must keep RAISING and let the retried turn get the
    COMPLETE harness. Dropping here would silently run one turn without the
    extension's globally-enabled instructions and produce no retry.

The flip is driven through the REAL state-mutation paths from inside a
post-synthesis-gate per-extension call, so no sleeps are needed and the
predicate under test (`is_extension_runtime_ready` / the `enabled` flag) is
never patched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-readiness-race-")

import config_store
import extension_store
import harness_fields
import harness_profile_resolver
import installation_profile

installation_profile.integrations_enabled = lambda: True

EXT_A = "fixture.race-a"
EXT_B = "fixture.race-b"
EXT_C = "fixture.race-c"
B_INSTRUCTION_NAME = "race-b-rules"
B_INSTRUCTION_BODY = "Race B instruction body."
C_LLM_TASK = "fixture_race_task"


def _install_fixture(
    extension_id: str,
    *,
    mcp_name: str,
    instruction_name: str = "",
    internal_llm_tasks: tuple[str, ...] = (),
) -> None:
    """Installs a minimal runtime-ready extension straight through the store
    internals (same fixture pattern as test_harness_profile_resolver_default),
    so the real readiness predicates and the real settings/instruction reads
    run against it."""
    package = Path(_TMP_HOME) / extension_id
    package.mkdir(parents=True, exist_ok=True)
    entrypoints: dict[str, object] = {
        "backend": "",
        "frontend": "",
        "mcp": [{"name": mcp_name, "command": "race-fixture", "args": [], "env": {}}],
        "provider_capabilities": [],
        "frontend_modules": [],
        "settings": [],
    }
    if instruction_name:
        instructions_dir = package / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)
        (instructions_dir / f"{instruction_name}.md").write_text(
            B_INSTRUCTION_BODY, encoding="utf-8"
        )
        entrypoints["instructions"] = [{
            "name": instruction_name,
            "path": f"instructions/{instruction_name}.md",
            "level": "global",
        }]
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": entrypoints,
        "permissions": (
            {"internal_llm_tasks": list(internal_llm_tasks)} if internal_llm_tasks else {}
        ),
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
    }
    validated = extension_store.validate_manifest(manifest)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/race.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-fixture",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    # Non-builtin extensions need recorded permission consent before
    # set_enabled(True) will re-enable them (the real user path).
    extension_store.grant_consent(extension_id)


def _flip_on_first_user_instructions_read(flip) -> object:
    """Installs a wrapper around `extension_store.get_user_instructions` that
    runs `flip()` exactly once, on its first call.

    `get_user_instructions` is read in compute_default_profile's SECOND loop,
    i.e. strictly after every extension has already passed the synthesis
    readiness gate, and strictly before resolve_for_session's per-instance
    second read. That places the flip precisely inside the race window with no
    timing dependency.
    """
    original = extension_store.get_user_instructions
    state = {"fired": False}

    def wrapped(extension_id: str) -> str:
        if not state["fired"]:
            state["fired"] = True
            flip()
        return original(extension_id)

    extension_store.get_user_instructions = wrapped  # type: ignore[assignment]
    return original


def _restore_user_instructions(original) -> None:
    extension_store.get_user_instructions = original  # type: ignore[assignment]


def test_durable_mid_resolve_disable_drops_the_instance() -> None:
    _install_fixture(EXT_A, mcp_name="race-a-server")
    _install_fixture(EXT_B, mcp_name="race-b-server", instruction_name=B_INSTRUCTION_NAME)
    assert extension_store.is_extension_runtime_ready(EXT_A)
    assert extension_store.is_extension_runtime_ready(EXT_B)
    harness_profile_resolver.invalidate_cache()

    baseline = harness_profile_resolver.resolve_for_session({})
    assert EXT_B in baseline["extension_mcp_servers"], "fixture B is not selected before the flip"
    assert any(
        block["name"] == B_INSTRUCTION_NAME for block in baseline["instruction_blocks"]
    ), "fixture B's instruction block is not present before the flip"
    harness_profile_resolver.invalidate_cache()

    # Real durable disable path -- exactly the state auto-quarantine writes.
    original = _flip_on_first_user_instructions_read(
        lambda: extension_store.set_enabled(EXT_B, False)
    )
    try:
        snapshot = harness_profile_resolver.resolve_for_session({})
    finally:
        _restore_user_instructions(original)

    assert snapshot is not None
    assert EXT_A in snapshot["extension_mcp_servers"], (
        "a durable flip on one extension must not remove the others"
    )
    assert EXT_B not in snapshot["extension_mcp_servers"]
    assert EXT_B not in snapshot["extension_revisions"]
    assert snapshot["dropped_extension_ids"] == [EXT_B], (
        f"dropped extension not surfaced: {snapshot['dropped_extension_ids']!r}"
    )
    assert B_INSTRUCTION_NAME in snapshot["dropped_instruction_names"]
    assert all(
        block["name"] != B_INSTRUCTION_NAME for block in snapshot["instruction_blocks"]
    ), "a dropped extension's instructions were still injected"
    assert B_INSTRUCTION_BODY not in json.dumps(snapshot["capability_contexts"])
    assert "race-b-server" not in snapshot["extra_mcp_servers"]


def test_dropped_extension_user_instructions_are_omitted() -> None:
    """An extension's free-text user instructions are keyed by
    harness_fields.user_instruction_source_name, so they must drop with the
    extension -- otherwise the run describes capabilities it does not have."""
    extension_store.set_enabled(EXT_B, True)
    extension_store.set_user_instructions(EXT_B, "Race B user instructions.")
    harness_profile_resolver.invalidate_cache()
    user_source_name = harness_fields.user_instruction_source_name(EXT_B)

    baseline = harness_profile_resolver.resolve_for_session({})
    assert any(block["name"] == user_source_name for block in baseline["instruction_blocks"])
    harness_profile_resolver.invalidate_cache()

    original = _flip_on_first_user_instructions_read(
        lambda: extension_store.set_enabled(EXT_B, False)
    )
    try:
        snapshot = harness_profile_resolver.resolve_for_session({})
    finally:
        _restore_user_instructions(original)

    assert snapshot["dropped_extension_ids"] == [EXT_B]
    assert user_source_name in snapshot["dropped_instruction_names"]
    assert all(block["name"] != user_source_name for block in snapshot["instruction_blocks"])

    extension_store.set_user_instructions(EXT_B, "")
    harness_profile_resolver.invalidate_cache()


def test_degraded_snapshot_is_not_cached() -> None:
    """The drop reflects live state at one instant, not the cache key (the
    store fingerprint is TTL-cached), so a degraded snapshot must never be
    replayed to a later turn."""
    harness_profile_resolver.invalidate_cache()
    original = _flip_on_first_user_instructions_read(
        lambda: extension_store.set_enabled(EXT_B, True)
    )
    try:
        # Re-enable mid-resolve: B was gated out of the synthesis while
        # disabled, so this resolve is not degraded and IS cacheable.
        harness_profile_resolver.resolve_for_session({})
    finally:
        _restore_user_instructions(original)

    harness_profile_resolver.invalidate_cache()
    original = _flip_on_first_user_instructions_read(
        lambda: extension_store.set_enabled(EXT_B, False)
    )
    try:
        degraded = harness_profile_resolver.resolve_for_session({})
    finally:
        _restore_user_instructions(original)
    assert degraded["dropped_extension_ids"] == [EXT_B]

    # Same cache key inputs, no invalidate: the degraded snapshot must not be
    # served again -- B is now durably disabled, so it is gated out of the
    # synthesis and simply absent, never "dropped".
    fresh = harness_profile_resolver.resolve_for_session({})
    assert fresh["dropped_extension_ids"] == [], (
        "a degraded snapshot was cached and replayed"
    )
    assert EXT_B not in fresh["extension_mcp_servers"]


def test_recoverable_mid_resolve_unreadiness_still_raises() -> None:
    """C stays `enabled` but loses its internal-LLM resolution mid-resolve.
    That recovers with no user action, so the turn MUST fail and be retried
    with the complete harness."""
    _install_fixture(
        EXT_C, mcp_name="race-c-server", internal_llm_tasks=(C_LLM_TASK,)
    )
    provider_home = Path(_TMP_HOME) / "provider-home"
    provider_home.mkdir(parents=True, exist_ok=True)
    state = config_store._load_state()  # type: ignore[attr-defined]
    # Append, never replace: a seeded test home may already carry providers and
    # dropping them makes config_store log spurious provider removals.
    state["providers"] = [
        *(state.get("providers") or []),
        {
            "id": "race-claude",
            "kind": "claude",
            "name": "Claude",
            "config_dir": str(provider_home),
            "default_model": "claude-opus-4-8",
        },
        # A provider with no default model: an internal-LLM task pinned to it
        # resolves to a provider but no model, i.e. exactly the transient
        # not-ready state an in-flight reassignment produces.
        {
            "id": "race-unassigned",
            "kind": "claude",
            "name": "Claude Unassigned",
            "config_dir": str(provider_home),
            "default_model": "",
        },
    ]
    state["default_provider_id"] = "race-claude"
    config_store._save_state(state)  # type: ignore[attr-defined]

    assert extension_store.is_extension_runtime_ready(EXT_C), (
        "fixture C is not runtime-ready with an internal-LLM provider configured"
    )
    harness_profile_resolver.invalidate_cache()
    baseline = harness_profile_resolver.resolve_for_session({})
    assert EXT_C in baseline["extension_mcp_servers"]
    harness_profile_resolver.invalidate_cache()

    def reassign_internal_llm() -> None:
        # Real assignment-write path (PATCH /api/internal-llm uses it).
        config_store.set_internal_llm_assignments(
            {C_LLM_TASK: {"provider_id": "race-unassigned", "model": ""}}
        )

    original = _flip_on_first_user_instructions_read(reassign_internal_llm)
    try:
        harness_profile_resolver.resolve_for_session({})
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        assert EXT_C in str(exc), f"unexpected resolution error: {exc}"
    else:
        raise AssertionError(
            "a recoverable mid-resolve unreadiness was silently dropped; the "
            "turn would run without the extension and never retry"
        )
    finally:
        _restore_user_instructions(original)
        config_store.set_internal_llm_assignments({})
        harness_profile_resolver.invalidate_cache()

    # The extension stayed `enabled` throughout, so it is still selected once
    # the assignment resolves again -- proving the raise was the recoverable
    # branch, not a durable drop.
    assert extension_store.is_extension_runtime_ready(EXT_C)
    recovered = harness_profile_resolver.resolve_for_session({})
    assert EXT_C in recovered["extension_mcp_servers"]
    assert recovered["dropped_extension_ids"] == []


def main() -> int:
    test_durable_mid_resolve_disable_drops_the_instance()
    test_dropped_extension_user_instructions_are_omitted()
    test_degraded_snapshot_is_not_cached()
    test_recoverable_mid_resolve_unreadiness_still_raises()
    print("PASS harness profile readiness race: durable drop, recoverable raise")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
