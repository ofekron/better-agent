from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-resolver-default-")

import config_store
import extension_store
import harness_profile_resolver
import harness_profile_store
import installation_profile

installation_profile.integrations_enabled = lambda: True

_FIXTURE_BROWSER_HARNESS_EXTENSION_ID = "fixture.browser-harness"


def _install_browser_harness_extension_with_headless_setting() -> None:
    """Installs a minimal runtime-ready browserHarness-role extension whose
    manifest declares the "headless" boolean setting, matching the shape the
    real browser-harness extension's settings schema uses. Written directly
    via the store internals (same pattern as other fixture installs in this
    test suite), so PATCH /api/extensions/{id}/settings and
    compute_default_profile() exercise the real settings read/write path."""
    package = Path(_TMP_HOME) / "browser-harness-extension"
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": _FIXTURE_BROWSER_HARNESS_EXTENSION_ID,
        "core_roles": ["browser-harness"],
        "name": "Browser Harness",
        "version": "1.0.0",
        "description": "Browser Harness",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [
                {"key": "headless", "label": "Headless", "type": "boolean", "default": False},
            ],
        },
        "permissions": {},
        "marketplace": {},
    }
    manifest.setdefault("protocol", {
        "version": 1,
        "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
    })
    validated = extension_store.validate_manifest(manifest)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/browser-harness",
            "ref": "",
            "commit_sha": "browser-harness-fixture",
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


def test_zero_override_profile_resolves_identically_to_default() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    config_store.set_disabled_builtin_extensions([])
    harness_profile_store.create_profile({"id": "zero.override", "name": "Zero Override"})
    default_resolved = harness_profile_resolver.resolve_profile("default")
    named_resolved = harness_profile_resolver.resolve_profile("zero.override")
    assert named_resolved["extension_instances"] == default_resolved["extension_instances"]
    assert named_resolved["disabled_builtin_tools"]["resolved"] == default_resolved["disabled_builtin_tools"]["resolved"]
    assert named_resolved["disabled_builtin_tools"]["override"] is None
    assert named_resolved["disabled_builtin_extensions"]["resolved"] == default_resolved["disabled_builtin_extensions"]["resolved"]
    assert named_resolved["disabled_builtin_extensions"]["override"] is None


def test_overridden_field_stays_pinned_across_default_change() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    harness_profile_store.create_profile({"id": "pinned.tools", "name": "Pinned Tools"})
    harness_profile_store.apply_override_patch(
        "pinned.tools",
        [{
            "path": ["disabled_builtin_tools"],
            "op": "set",
            "value": {"add": ["mssg"], "remove": []},
        }],
    )
    before = harness_profile_resolver.resolve_profile("pinned.tools")
    assert set(before["disabled_builtin_tools"]["resolved"]) == {"ask", "mssg"}
    assert before["disabled_builtin_tools"]["override"] == {"add": ["mssg"], "remove": []}

    # Mutate live Default's disabled_builtin_tools out from under the profile.
    config_store.set_disabled_builtin_tools(["create_session", "delegate_task"])

    after = harness_profile_resolver.resolve_profile("pinned.tools")
    # The override recomputes as a delta over the NEW Default base (the
    # override's "add" is still honored; it's not a frozen snapshot of the
    # merged list) but the override delta itself (what the user actually
    # set) must stay pinned, unaffected by the Default mutation.
    assert after["disabled_builtin_tools"]["override"] == {"add": ["mssg"], "remove": []}
    assert "mssg" in after["disabled_builtin_tools"]["resolved"]


def test_unoverridden_field_tracks_default_live() -> None:
    config_store.set_disabled_builtin_extensions([])
    harness_profile_store.create_profile({"id": "tracks.default", "name": "Tracks Default"})
    before = harness_profile_resolver.resolve_profile("tracks.default")
    assert before["disabled_builtin_extensions"]["resolved"] == []
    assert before["disabled_builtin_extensions"]["override"] is None

    config_store.set_disabled_builtin_extensions(["ofek-dev.todos"])

    after = harness_profile_resolver.resolve_profile("tracks.default")
    assert after["disabled_builtin_extensions"]["resolved"] == ["ofek-dev.todos"]
    assert after["disabled_builtin_extensions"]["override"] is None


def test_clearing_override_reverts_to_tracking_default() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    harness_profile_store.create_profile({"id": "clear.me", "name": "Clear Me"})
    harness_profile_store.apply_override_patch(
        "clear.me",
        [{
            "path": ["disabled_builtin_tools"],
            "op": "set",
            "value": {"add": ["mssg"], "remove": []},
        }],
    )
    overridden = harness_profile_resolver.resolve_profile("clear.me")
    assert overridden["disabled_builtin_tools"]["override"] is not None

    harness_profile_store.apply_override_patch(
        "clear.me",
        [{"path": ["disabled_builtin_tools"], "op": "clear"}],
    )
    cleared = harness_profile_resolver.resolve_profile("clear.me")
    assert cleared["disabled_builtin_tools"]["override"] is None
    assert cleared["disabled_builtin_tools"]["resolved"] == ["ask"]

    config_store.set_disabled_builtin_tools(["ask", "create_session"])
    tracked = harness_profile_resolver.resolve_profile("clear.me")
    assert tracked["disabled_builtin_tools"]["resolved"] == ["ask", "create_session"]


def test_default_headless_reflects_extension_setting_write() -> None:
    _install_browser_harness_extension_with_headless_setting()
    assert extension_store.is_extension_runtime_ready(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID)

    before = harness_profile_resolver.compute_default_profile()
    assert before["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is False

    # Same write path PATCH /api/extensions/{id}/settings uses.
    extension_store.set_extension_setting(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID, "headless", True)

    after = harness_profile_resolver.compute_default_profile()
    assert after["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is True


def test_resolve_for_session_falls_back_to_default_profile() -> None:
    """A session/turn with no explicit harness_profile_id (the case for every
    session created before profile selection existed, and the overwhelming
    majority today) must still resolve a snapshot -- via the Default profile
    -- not collapse `resolved_harness_run_config` to `{}`. Regression test
    for the bug where `resolve_for_session` returned None whenever no
    profile was explicitly selected, which zeroed
    `launcher_projection.extension_mcp_servers` for every ordinary session
    and dropped every extension's MCP servers from every turn."""
    snapshot = harness_profile_resolver.resolve_for_session({})
    assert snapshot is not None, (
        "resolve_for_session must fall back to the Default profile, not return None, "
        "when no harness_profile_id is set on the call or the session"
    )
    assert snapshot["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID
    launcher_projection = snapshot["launcher_projection"]
    assert isinstance(launcher_projection.get("extension_mcp_servers"), dict)

    # A session record that also has no harness_profile_id (the persisted
    # field default) must resolve the same way.
    snapshot_from_session = harness_profile_resolver.resolve_for_session(
        {"harness_profile_id": "", "harness_profile_revision": ""}
    )
    assert snapshot_from_session is not None
    assert snapshot_from_session["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID

    # An explicit profile_id still takes priority over the fallback.
    harness_profile_store.create_profile({"id": "explicit.profile", "name": "Explicit"})
    explicit_snapshot = harness_profile_resolver.resolve_for_session(
        {}, profile_id="explicit.profile",
    )
    assert explicit_snapshot["profile_id"] == "explicit.profile"


def test_multi_level_base_chain_applies_deltas_in_order() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    for pid, name in (("chain.a", "A"), ("chain.b", "B"), ("chain.c", "C")):
        harness_profile_store.create_profile({"id": pid, "name": name})
    # A over Default adds mssg; B over A adds delegate_task; C over B removes ask.
    harness_profile_store.apply_override_patch(
        "chain.a", [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": ["mssg"], "remove": []}}]
    )
    harness_profile_store.set_profile_meta("chain.b", {"base_profile_id": "chain.a"})
    harness_profile_store.apply_override_patch(
        "chain.b", [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": ["delegate_task"], "remove": []}}]
    )
    harness_profile_store.set_profile_meta("chain.c", {"base_profile_id": "chain.b"})
    harness_profile_store.apply_override_patch(
        "chain.c", [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": [], "remove": ["ask"]}}]
    )

    resolved_a = harness_profile_resolver.resolve_profile("chain.a")
    resolved_b = harness_profile_resolver.resolve_profile("chain.b")
    resolved_c = harness_profile_resolver.resolve_profile("chain.c")
    assert set(resolved_a["disabled_builtin_tools"]["resolved"]) == {"ask", "mssg"}
    assert set(resolved_b["disabled_builtin_tools"]["resolved"]) == {"ask", "mssg", "delegate_task"}
    # C inherits A's mssg and B's delegate_task through the chain, and removes ask.
    assert set(resolved_c["disabled_builtin_tools"]["resolved"]) == {"mssg", "delegate_task"}
    assert resolved_c["base_profile_id"] == "chain.b"


def test_resolve_time_cycle_detected() -> None:
    harness_profile_store.create_profile({"id": "rcyc.a", "name": "RCyc A"})
    harness_profile_store.create_profile({"id": "rcyc.b", "name": "RCyc B"})
    harness_profile_store.set_profile_meta("rcyc.b", {"base_profile_id": "rcyc.a"})
    # Force a cycle directly in the store, bypassing the save-time guard, to
    # prove resolve_profile also fails closed on a cyclic chain.
    data = harness_profile_store._load()  # type: ignore[attr-defined]
    data["profiles"]["rcyc.a"]["base_profile_id"] = "rcyc.b"
    harness_profile_store._save(data)  # type: ignore[attr-defined]
    try:
        harness_profile_resolver.resolve_profile("rcyc.a")
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("resolve_profile did not detect the base cycle")


def test_pin_inherited_from_grandparent_when_child_pins_none() -> None:
    for pid, name in (("pin.g", "G"), ("pin.p", "P"), ("pin.c", "C")):
        harness_profile_store.create_profile({"id": pid, "name": name})
    harness_profile_store.set_profile_meta(
        "pin.g", {"default_provider_id": "codex", "default_model": "gpt-5.5", "default_reasoning_effort": "high"}
    )
    harness_profile_store.set_profile_meta("pin.p", {"base_profile_id": "pin.g"})
    harness_profile_store.set_profile_meta("pin.c", {"base_profile_id": "pin.p"})
    pins = harness_profile_resolver.profile_selector_defaults("pin.c")
    assert pins == {"provider_id": "codex", "model": "gpt-5.5", "reasoning_effort": "high"}


def test_own_pin_overrides_inherited_pin() -> None:
    harness_profile_store.create_profile({"id": "pin.base2", "name": "Base2"})
    harness_profile_store.create_profile({"id": "pin.own", "name": "Own"})
    harness_profile_store.set_profile_meta(
        "pin.base2", {"default_provider_id": "codex", "default_model": "gpt-5.5"}
    )
    harness_profile_store.set_profile_meta(
        "pin.own", {"base_profile_id": "pin.base2", "default_model": "gpt-5.5-mini"}
    )
    pins = harness_profile_resolver.profile_selector_defaults("pin.own")
    # provider inherited from base; model is the child's own pin.
    assert pins == {"provider_id": "codex", "model": "gpt-5.5-mini", "reasoning_effort": None}


def test_merge_selector_defaults_explicit_wins_only_where_present() -> None:
    harness_profile_store.create_profile({"id": "merge.p", "name": "Merge"})
    harness_profile_store.set_profile_meta(
        "merge.p", {"default_provider_id": "codex", "default_model": "gpt-5.5", "default_reasoning_effort": "high"}
    )
    # Caller supplies model explicitly; omits provider/effort -> pins fill those only.
    merged = harness_profile_resolver.merge_selector_defaults(
        {"provider_id": None, "model": "claude-opus-4-8", "reasoning_effort": None}, "merge.p"
    )
    assert merged == {"provider_id": "codex", "model": "claude-opus-4-8", "reasoning_effort": "high"}
    # No profile -> identity, pins never consulted.
    none_merged = harness_profile_resolver.merge_selector_defaults(
        {"provider_id": None, "model": None, "reasoning_effort": None}, ""
    )
    assert none_merged == {"provider_id": None, "model": None, "reasoning_effort": None}


def main() -> int:
    test_zero_override_profile_resolves_identically_to_default()
    test_overridden_field_stays_pinned_across_default_change()
    test_unoverridden_field_tracks_default_live()
    test_clearing_override_reverts_to_tracking_default()
    test_default_headless_reflects_extension_setting_write()
    test_resolve_for_session_falls_back_to_default_profile()
    test_multi_level_base_chain_applies_deltas_in_order()
    test_resolve_time_cycle_detected()
    test_pin_inherited_from_grandparent_when_child_pins_none()
    test_own_pin_overrides_inherited_pin()
    test_merge_selector_defaults_explicit_wins_only_where_present()
    print("PASS harness profile resolver default synthesis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
