from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

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
import password_manager

installation_profile.integrations_enabled = lambda: True

_FIXTURE_BROWSER_HARNESS_EXTENSION_ID = "fixture.browser-harness"
_FIXTURE_SECRET_SETTING_EXTENSION_ID = "fixture.secret-setting"
_FIXTURE_SECRET_SETTING_EXTENSION_ID_2 = "fixture.secret-setting-2"


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


def _install_secret_setting_extension(extension_id: str) -> None:
    """Installs a minimal runtime-ready extension with a secret-typed
    setting, so its settings write path exercises the OS-keychain probe
    inside extension_store.get_extension_settings -> compute_default_profile
    (secret_refs), same as a real credential-backed extension. Each caller
    must pass its own extension_id: extension_store._save() deliberately
    refuses to resurrect an id a prior uninstall() just removed unless
    resurrect_extension_ids names it, so reusing one id across tests that
    each install-then-uninstall would silently no-op the second install."""
    package = Path(_TMP_HOME) / f"{extension_id}-package"
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": "Secret Setting Fixture",
        "version": "1.0.0",
        "description": "Secret Setting Fixture",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [
                {"key": "api_key", "label": "API key", "type": "secret"},
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
    data["extensions"][extension_id] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": "secret-setting-fixture",
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
    extension_store._save(data, resurrect_extension_ids={extension_id})  # type: ignore[attr-defined]


def test_secret_setting_write_eagerly_warms_default_profile_cache() -> None:
    """Regression for turns crashing on a locked/slow OS keychain: a secret
    setting write must pay the compute_default_profile() keychain-probe
    cost itself (extension_store._invalidate_harness_profile_resolver_cache
    now eagerly recomputes after invalidating), so the next unrelated turn's
    resolve_for_session -> resolve_profile -> compute_default_profile() is a
    warm cache hit and never touches password_manager."""
    _install_secret_setting_extension(_FIXTURE_SECRET_SETTING_EXTENSION_ID)
    try:
        assert extension_store.is_extension_runtime_ready(_FIXTURE_SECRET_SETTING_EXTENSION_ID)
        harness_profile_resolver.invalidate_cache()

        stored: dict[str, str] = {}
        probe_calls = 0

        def counted_has_service_password(service: str, account: str) -> bool:
            nonlocal probe_calls
            probe_calls += 1
            return account in stored

        def fake_store_service_password(payload: dict) -> dict:
            stored[payload["account"]] = payload["password"]
            return {"service": payload["service"], "account": payload["account"]}

        with (
            patch("password_manager.has_service_password", side_effect=counted_has_service_password),
            patch("password_manager.store_service_password", side_effect=fake_store_service_password),
        ):
            extension_store.set_extension_setting(
                _FIXTURE_SECRET_SETTING_EXTENSION_ID, "api_key", "fixture-secret-value",
            )
            assert probe_calls > 0, "settings write should have probed the keychain at least once"
            calls_after_write = probe_calls

            # The write already resynthesized compute_default_profile() (and its
            # secret_refs, which is what actually needs secret_present). The next
            # caller — standing in for the next turn's resolve_for_session — must
            # be a pure cache hit: zero further keychain probes.
            default_profile = harness_profile_resolver.compute_default_profile()
            assert probe_calls == calls_after_write, (
                "compute_default_profile() after a settings write must not "
                "re-probe the OS keychain — the write should have already "
                "warmed the cache"
            )
            assert (
                f"extension-setting:{_FIXTURE_SECRET_SETTING_EXTENSION_ID}:api_key"
                in default_profile["extension_instances"][_FIXTURE_SECRET_SETTING_EXTENSION_ID][
                    "secret_refs"
                ]
            )
    finally:
        # Must fully remove the fixture (not just leave it installed): any
        # later test's unmocked compute_default_profile() would otherwise
        # probe the real OS keychain for this extension's secret setting.
        extension_store.uninstall(_FIXTURE_SECRET_SETTING_EXTENSION_ID)
        harness_profile_resolver.invalidate_cache()


def test_secret_setting_write_reports_saved_when_cache_warm_fails() -> None:
    """The secret write (password_manager.store_service_password) durably
    succeeds before the eager compute_default_profile() resynthesis runs. If
    that resynthesis fails (e.g. the OS keychain times out), the caller must
    see a normal extension_store.ExtensionError that says the value was
    saved — not a raw RuntimeError that reads as "your write failed, retry
    it" when it actually already landed."""
    _install_secret_setting_extension(_FIXTURE_SECRET_SETTING_EXTENSION_ID_2)
    try:
        harness_profile_resolver.invalidate_cache()
        store_calls: list[dict] = []

        def fake_store_service_password(payload: dict) -> dict:
            store_calls.append(payload)
            return {"service": payload["service"], "account": payload["account"]}

        def failing_has_service_password(*_args, **_kwargs) -> bool:
            raise RuntimeError("OS credential read timed out")

        with (
            patch("password_manager.store_service_password", side_effect=fake_store_service_password),
            patch("password_manager.has_service_password", side_effect=failing_has_service_password),
        ):
            try:
                extension_store.set_extension_setting(
                    _FIXTURE_SECRET_SETTING_EXTENSION_ID_2, "api_key", "fixture-secret-value",
                )
            except extension_store.ExtensionError as exc:
                assert "was saved" in str(exc), f"error should say the value was saved: {exc}"
            else:
                raise AssertionError(
                    "expected extension_store.ExtensionError when the eager cache "
                    "warm fails, got no exception"
                )
        assert len(store_calls) == 1, "the secret write itself must still have gone through"
    finally:
        extension_store.uninstall(_FIXTURE_SECRET_SETTING_EXTENSION_ID_2)
        harness_profile_resolver.invalidate_cache()


def test_default_profile_cache_key_ignores_unrelated_writes() -> None:
    """_default_profile_cache_key() must track only what
    _compute_default_profile_uncached actually reads (config_store's
    disabled_builtin_tools/extensions, plus the extension store). Before this
    fix it also fingerprinted the WHOLE config.json (which also holds
    provider/session state) and harness_profile_store's named-profile store
    (never read by the default-profile synthesis at all) — so an unrelated
    write to either would spuriously invalidate _DEFAULT_PROFILE_CACHE and
    force the next caller (possibly a turn) to pay a full resynthesis,
    including an OS-keychain probe per extension secret."""
    harness_profile_resolver.invalidate_cache()
    harness_profile_resolver.compute_default_profile()
    key_before = harness_profile_resolver._default_profile_cache_key()

    original_policy = config_store.get_delegate_task_policy()
    other_policy = "manual" if original_policy != "manual" else "auto"
    config_store.set_delegate_task_policy(other_policy)
    try:
        assert harness_profile_resolver._default_profile_cache_key() == key_before, (
            "an unrelated config_store write (delegate_task_policy) must not "
            "change the default-profile cache key"
        )

        created = harness_profile_store.create_profile({"name": "unrelated-fixture-profile"})
        try:
            assert harness_profile_resolver._default_profile_cache_key() == key_before, (
                "creating a named harness profile must not change the "
                "default-profile cache key — it isn't read by "
                "_compute_default_profile_uncached"
            )
        finally:
            harness_profile_store.delete_profile(created["id"])

        # A genuine dependency change must still invalidate — this is the
        # under-invalidation guard: narrowing the key must not silently drop
        # a real one.
        original_disabled = config_store.get_disabled_builtin_tools()
        config_store.set_disabled_builtin_tools(["ask"])
        try:
            assert harness_profile_resolver._default_profile_cache_key() != key_before, (
                "changing disabled_builtin_tools must still invalidate the "
                "default-profile cache key"
            )
        finally:
            config_store.set_disabled_builtin_tools(original_disabled)
    finally:
        config_store.set_delegate_task_policy(original_policy)
        harness_profile_resolver.invalidate_cache()


def test_default_headless_reflects_extension_setting_write() -> None:
    _install_browser_harness_extension_with_headless_setting()
    assert extension_store.is_extension_runtime_ready(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID)

    before = harness_profile_resolver.compute_default_profile()
    assert before["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is False

    # Same write path PATCH /api/extensions/{id}/settings uses.
    extension_store.set_extension_setting(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID, "headless", True)

    after = harness_profile_resolver.compute_default_profile()
    assert after["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is True


def test_default_profile_synthesis_is_cached_until_invalidated() -> None:
    harness_profile_resolver.invalidate_cache()
    default_calls = 0
    resolve_calls = 0
    original_default = harness_profile_resolver._compute_default_profile_uncached
    original_resolve = harness_profile_resolver.resolve_profile

    def counted() -> dict:
        nonlocal default_calls
        default_calls += 1
        return original_default()

    def counted_resolve(*args, **kwargs) -> dict:
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(*args, **kwargs)

    harness_profile_resolver._compute_default_profile_uncached = counted
    harness_profile_resolver.resolve_profile = counted_resolve
    try:
        first = harness_profile_resolver.resolve_for_session({})
        second = harness_profile_resolver.resolve_for_session({})
        assert first["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID
        assert second["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID
        assert default_calls == 1
        assert resolve_calls == 1

        harness_profile_resolver.invalidate_cache()
        third = harness_profile_resolver.resolve_for_session({})
        assert third["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID
        assert default_calls == 2
        assert resolve_calls == 2
    finally:
        harness_profile_resolver._compute_default_profile_uncached = original_default
        harness_profile_resolver.resolve_profile = original_resolve
        harness_profile_resolver.invalidate_cache()


def test_run_snapshot_cache_tracks_selected_package_fingerprint() -> None:
    _install_browser_harness_extension_with_headless_setting()
    harness_profile_resolver.invalidate_cache()
    resolve_calls = 0
    package_fingerprint = "package-a"
    original_resolve = harness_profile_resolver.resolve_profile
    original_package_fingerprint = extension_store.runtime_package_content_fingerprint

    def counted_resolve(*args, **kwargs) -> dict:
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(*args, **kwargs)

    def package_fingerprint_for_record(_record: dict) -> str:
        return package_fingerprint

    harness_profile_resolver.resolve_profile = counted_resolve
    extension_store.runtime_package_content_fingerprint = package_fingerprint_for_record
    try:
        first = harness_profile_resolver.resolve_for_session({})
        second = harness_profile_resolver.resolve_for_session({})
        assert _FIXTURE_BROWSER_HARNESS_EXTENSION_ID in first["extension_revisions"]
        assert _FIXTURE_BROWSER_HARNESS_EXTENSION_ID in second["extension_revisions"]
        assert resolve_calls == 1

        package_fingerprint = "package-b"
        third = harness_profile_resolver.resolve_for_session({})
        assert _FIXTURE_BROWSER_HARNESS_EXTENSION_ID in third["extension_revisions"]
        assert resolve_calls == 2
    finally:
        harness_profile_resolver.resolve_profile = original_resolve
        extension_store.runtime_package_content_fingerprint = original_package_fingerprint
        harness_profile_resolver.invalidate_cache()


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
        {"harness_profile_id": ""}
    )
    assert snapshot_from_session is not None
    assert snapshot_from_session["profile_id"] == harness_profile_store.DEFAULT_PROFILE_ID

    # An explicit profile_id still takes priority over the fallback.
    harness_profile_store.create_profile({"id": "explicit.profile", "name": "Explicit"})
    explicit_snapshot = harness_profile_resolver.resolve_for_session(
        {}, profile_id="explicit.profile",
    )
    assert explicit_snapshot["profile_id"] == "explicit.profile"


def test_profile_can_disable_runtime_skills_for_run_snapshot() -> None:
    harness_profile_store.create_profile({"id": "no.skills", "name": "No Skills"})
    harness_profile_store.apply_override_patch(
        "no.skills",
        [{"path": ["disabled_runtime_skills"], "op": "set", "value": {"add": ["*"], "remove": []}}],
    )

    resolved = harness_profile_resolver.resolve_profile("no.skills")
    assert resolved["disabled_runtime_skills"]["resolved"] == ["*"]
    snapshot = harness_profile_resolver.resolve_for_session({"harness_profile_id": "no.skills"})
    assert snapshot["disabled_runtime_skills"] == ["*"]
    assert snapshot["launcher_projection"]["disabled_runtime_skills"] == ["*"]


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
    harness_profile_store._write_profile_row(data["profiles"]["rcyc.a"])  # type: ignore[attr-defined]
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
    test_secret_setting_write_eagerly_warms_default_profile_cache()
    test_secret_setting_write_reports_saved_when_cache_warm_fails()
    test_default_profile_cache_key_ignores_unrelated_writes()
    test_default_profile_synthesis_is_cached_until_invalidated()
    test_run_snapshot_cache_tracks_selected_package_fingerprint()
    test_resolve_for_session_falls_back_to_default_profile()
    test_profile_can_disable_runtime_skills_for_run_snapshot()
    test_multi_level_base_chain_applies_deltas_in_order()
    test_resolve_time_cycle_detected()
    test_pin_inherited_from_grandparent_when_child_pins_none()
    test_own_pin_overrides_inherited_pin()
    test_merge_selector_defaults_explicit_wins_only_where_present()
    print("PASS harness profile resolver default synthesis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
