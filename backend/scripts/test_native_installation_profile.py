from __future__ import annotations

import json
import os

import pytest

import installation_capabilities
import installation_profile
import provider_setup
from bundled_extensions import PUBLIC_EXTENSION_PATHS
from json_store import write_json_durable

HEX64 = "a" * 64


def _identity(
    command: str = "claude",
    launcher_path: str = "/usr/local/bin/claude",
    target_path: str = "/usr/local/lib/claude.bin",
    size: int = 1,
    mtime_ns: int = 1,
) -> dict:
    return {
        "command": command,
        "launcher_path": launcher_path,
        "launcher_sha256": HEX64,
        "target_sha256": HEX64,
        "target_path": target_path,
        "size": size,
        "mtime_ns": mtime_ns,
    }


def _active_profile(provider: str = "claude", identity: dict | None = None) -> dict:
    if identity is None:
        identity = _identity(command=provider)
    return installation_profile.new_active_profile(
        mode=installation_profile.DEFAULT,
        provider=provider,
        provider_identity=identity,
    )


def _stage_ready(profile: dict) -> None:
    """Stage the profile and write a matching receipt so _bootstrap_ready is True."""
    installation_profile.stage_activation(profile)
    write_json_durable(
        installation_profile._activation_receipt_path(),
        installation_profile._receipt(profile),
    )


@pytest.fixture(autouse=True)
def _isolate_profile(monkeypatch, tmp_path):
    # Isolate via BETTER_AGENT_HOME so the real _path()/_activation_receipt_path()
    # resolve under tmp_path (covers those wrappers honestly) and no production
    # state is touched. See CLAUDE.md "State directory isolation".
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(installation_profile, "_load_cache", None)
    installation_capabilities.forget_active()
    yield


def test_native_openai_profile_requires_no_executable_identity(monkeypatch, tmp_path):
    profile_path = tmp_path / "installation.json"
    receipt_path = tmp_path / "installation-activation.json"
    monkeypatch.setattr(installation_profile, "_path", lambda: profile_path)
    monkeypatch.setattr(
        installation_profile,
        "_activation_receipt_path",
        lambda: receipt_path,
    )

    profile = installation_profile.new_native_active_profile(
        mode=installation_profile.DEFAULT,
        provider="openai",
    )
    installation_profile.stage_activation(profile)

    loaded = installation_profile.load()
    assert loaded["status"] == "active"
    assert loaded["provider"] == "openai"
    assert loaded["provider_identity"] is None
    assert installation_profile.repin_provider_executable({}) is False


def test_native_openai_profile_seeds_public_bundled_extensions_without_local_marketplace(
    monkeypatch,
    tmp_path,
):
    import extension_store

    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE", "1")
    monkeypatch.delenv("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", raising=False)
    monkeypatch.setattr(installation_profile, "_assert_active_environment", lambda: None)
    installation_capabilities.forget_active()

    profile = installation_profile.new_native_active_profile(
        mode=installation_profile.DEFAULT,
        provider="openai",
    )
    installation_profile.stage_activation(profile)
    installation_profile.mark_selection_applied()

    try:
        records = {
            item["manifest"]["id"]: item
            for item in extension_store.list_extensions_with_reconciliation(
                include_hidden=True,
            )[0]
        }
        expected_ids = set(PUBLIC_EXTENSION_PATHS)
        assert expected_ids <= records.keys()
        assert {
            records[extension_id]["source"]["type"]
            for extension_id in expected_ids
        } == {"better_agent_bundled"}
        assert extension_store._local_required_marketplace_repo_root() is None
    finally:
        installation_capabilities.forget_active()


def test_desktop_package_uses_authoritative_public_extension_manifest():
    repo_root = installation_profile.BACKEND_ROOT.parent
    assert all(
        (repo_root / relative_path).is_dir()
        for relative_path in PUBLIC_EXTENSION_PATHS.values()
    )

    spec = (repo_root / "desktop" / "BetterAgent.spec").read_text(encoding="utf-8")
    assert "from bundled_extensions import PUBLIC_EXTENSION_PATHS" in spec
    assert "for relative_path in PUBLIC_EXTENSION_PATHS.values()" in spec


# --- _validate_identity -----------------------------------------------------

def test_validate_identity_accepts_well_formed_identity():
    parsed = installation_profile._validate_identity(_identity())
    assert parsed == _identity()


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-dict",
        {**_identity(), "extra": 1},
        {k: v for k, v in _identity().items() if k != "command"},
    ],
)
def test_validate_identity_rejects_non_dict_or_wrong_fields(bad):
    with pytest.raises(installation_profile.InstallationProfileError, match="identity"):
        installation_profile._validate_identity(bad)


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"command": ""}, "identity"),
        ({"command": 5}, "identity"),
        ({"launcher_path": ""}, "identity"),
        ({"target_path": "relative"}, "absolute"),
        ({"launcher_path": "relative"}, "absolute"),
    ],
)
def test_validate_identity_rejects_bad_string_fields(overrides, match):
    identity = {**_identity(), **overrides}
    with pytest.raises(installation_profile.InstallationProfileError, match=match):
        installation_profile._validate_identity(identity)


@pytest.mark.parametrize(
    "overrides",
    [
        {"launcher_sha256": "short"},
        {"launcher_sha256": "z" * 64},
        {"target_sha256": "z" * 64},
        {"launcher_sha256": 5},
    ],
)
def test_validate_identity_rejects_bad_digests(overrides):
    identity = {**_identity(), **overrides}
    with pytest.raises(installation_profile.InstallationProfileError, match="digest"):
        installation_profile._validate_identity(identity)


@pytest.mark.parametrize(
    "overrides",
    [
        {"size": -1},
        {"size": "big"},
        {"mtime_ns": -2},
        {"mtime_ns": 1.5},
    ],
)
def test_validate_identity_rejects_bad_metadata(overrides):
    identity = {**_identity(), **overrides}
    with pytest.raises(installation_profile.InstallationProfileError, match="metadata"):
        installation_profile._validate_identity(identity)


# --- _validate_active -------------------------------------------------------

def test_validate_active_native_and_non_native_roundtrip():
    native = installation_profile._validate_active(
        installation_profile.new_native_active_profile(
            mode=installation_profile.DEFAULT, provider="openai"
        )
    )
    assert native["provider_identity"] is None
    pinned = installation_profile._validate_active(_active_profile(provider="claude"))
    assert pinned["provider_identity"]["command"] == "claude"


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"schema_version": 1}, "schema"),
        ({"status": "setup_required"}, "not active"),
        ({"generation": ""}, "generation"),
        ({"generation": 5}, "generation"),
        ({"mode": "bogus"}, "mode"),
        ({"provider": 5}, "provider"),
        ({"provider": "unknown"}, "provider"),
    ],
)
def test_validate_active_rejects_malformed_fields(overrides, match):
    profile = _active_profile()
    profile.update(overrides)
    with pytest.raises(installation_profile.InstallationProfileError, match=match):
        installation_profile._validate_active(profile)


def test_validate_active_rejects_native_provider_with_identity():
    profile = installation_profile.new_native_active_profile(
        mode=installation_profile.DEFAULT, provider="openai"
    )
    profile["provider_identity"] = _identity(command="openai")
    with pytest.raises(installation_profile.InstallationProfileError, match="executable identity"):
        installation_profile._validate_active(profile)


def test_validate_active_rejects_identity_command_mismatch():
    profile = _active_profile(provider="claude")
    profile["provider_identity"]["command"] = "codex"
    with pytest.raises(installation_profile.InstallationProfileError, match="does not match"):
        installation_profile._validate_active(profile)


# --- load / require_active --------------------------------------------------

def test_load_returns_inactive_when_file_missing():
    loaded = installation_profile.load()
    assert loaded["status"] == "setup_required"
    assert loaded["reason"] == "missing"


def test_load_returns_inactive_when_file_invalid_json():
    installation_profile._path().write_text("{not json", encoding="utf-8")
    loaded = installation_profile.load()
    assert loaded["reason"] == "invalid"


def test_load_returns_inactive_when_profile_invalid():
    installation_profile._path().write_text(json.dumps({"status": "active"}), encoding="utf-8")
    loaded = installation_profile.load()
    assert loaded["reason"] == "invalid"


def test_load_caches_validated_result_across_calls(monkeypatch):
    installation_profile.stage_activation(_active_profile())
    installation_profile._load_cache = None  # ensure cold

    calls = 0
    real_validate = installation_profile._validate_active

    def counting(value):
        nonlocal calls
        calls += 1
        return real_validate(value)

    monkeypatch.setattr(installation_profile, "_validate_active", counting)
    first = installation_profile.load()
    second = installation_profile.load()
    assert first == second
    assert calls == 1  # second call served from cache, no re-validation


def test_require_active_raises_when_not_active():
    with pytest.raises(installation_profile.InstallationProfileError, match="setup is required"):
        installation_profile.require_active()


def test_require_active_returns_profile_when_active():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    assert installation_profile.require_active()["status"] == "active"


# --- profile constructors / stage ------------------------------------------

def test_new_native_active_profile_rejects_non_native_provider():
    with pytest.raises(installation_profile.InstallationProfileError, match="native"):
        installation_profile.new_native_active_profile(
            mode=installation_profile.DEFAULT, provider="claude"
        )


def test_stage_activation_writes_profile_and_clears_receipt():
    receipt = installation_profile._activation_receipt_path()
    write_json_durable(receipt, {"old": True})
    installation_profile.stage_activation(_active_profile())
    assert installation_profile._path().exists()
    assert not receipt.exists()


# --- _assert_active_environment (path-escape security guards) --------------

def test_assert_active_environment_short_circuits_when_frozen(monkeypatch):
    monkeypatch.setattr(installation_profile.sys, "frozen", True, raising=False)
    installation_profile._assert_active_environment()  # no raise


def _backend_root(monkeypatch, tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".venvs").mkdir()
    monkeypatch.setattr(installation_profile, "BACKEND_ROOT", backend)
    return backend


def test_assert_active_environment_rejects_missing_pointer(monkeypatch, tmp_path):
    _backend_root(monkeypatch, tmp_path)  # no .active-venv written
    with pytest.raises(installation_profile.InstallationProfileError, match="not active"):
        installation_profile._assert_active_environment()


@pytest.mark.parametrize("pointer", ["", "/abs/path", "..", "x/../y"])
def test_assert_active_environment_rejects_invalid_pointer(monkeypatch, tmp_path, pointer):
    backend = _backend_root(monkeypatch, tmp_path)
    (backend / ".active-venv").write_text(pointer, encoding="utf-8")
    with pytest.raises(installation_profile.InstallationProfileError, match="invalid"):
        installation_profile._assert_active_environment()


def test_assert_active_environment_rejects_pointer_that_escapes_venv_root(monkeypatch, tmp_path):
    backend = _backend_root(monkeypatch, tmp_path)
    os.symlink(tmp_path, backend / "evil")  # resolves outside .venvs
    (backend / ".active-venv").write_text("evil", encoding="utf-8")
    with pytest.raises(installation_profile.InstallationProfileError, match="escapes"):
        installation_profile._assert_active_environment()


@pytest.mark.parametrize(
    "marker, match",
    [
        (None, "marker"),  # missing file
        ("{bad", "marker"),  # bad json
        (json.dumps({"schema_version": 2, "hash": "h"}), "marker"),
        (json.dumps({"schema_version": 1, "hash": ""}), "marker"),
        (json.dumps({"schema_version": 1, "hash": 5}), "marker"),
    ],
)
def test_assert_active_environment_rejects_bad_plan_marker(monkeypatch, tmp_path, marker, match):
    backend = _backend_root(monkeypatch, tmp_path)
    env_dir = backend / ".venvs" / "venv1"
    env_dir.mkdir()
    if marker is not None:
        (env_dir / ".dependency-plan.json").write_text(marker, encoding="utf-8")
    (backend / ".active-venv").write_text(".venvs/venv1", encoding="utf-8")
    with pytest.raises(installation_profile.InstallationProfileError, match=match):
        installation_profile._assert_active_environment()


def test_assert_active_environment_accepts_valid_pointer_and_marker(monkeypatch, tmp_path):
    backend = _backend_root(monkeypatch, tmp_path)
    env_dir = backend / ".venvs" / "venv1"
    env_dir.mkdir()
    (env_dir / ".dependency-plan.json").write_text(
        json.dumps({"schema_version": 1, "hash": "abc"}), encoding="utf-8"
    )
    (backend / ".active-venv").write_text(".venvs/venv1", encoding="utf-8")
    installation_profile._assert_active_environment()  # no raise


# --- mark_selection_applied / refresh_activation_receipt -------------------

def test_mark_selection_applied_writes_receipt(monkeypatch):
    monkeypatch.setattr(installation_profile, "_assert_active_environment", lambda: None)
    installation_profile.stage_activation(_active_profile())
    installation_profile.mark_selection_applied()
    receipt = json.loads(installation_profile._activation_receipt_path().read_text())
    assert receipt["schema_version"] == installation_profile.RECEIPT_SCHEMA_VERSION
    assert "profile_sha256" in receipt


def test_refresh_activation_receipt_returns_false_when_not_active():
    assert installation_profile.refresh_activation_receipt() is False


def test_refresh_activation_receipt_returns_false_when_prior_missing():
    installation_profile.stage_activation(_active_profile())
    assert installation_profile.refresh_activation_receipt() is False  # no prior receipt


def test_refresh_activation_receipt_returns_false_for_prior_not_dict():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    installation_profile._activation_receipt_path().write_text("null", encoding="utf-8")
    assert installation_profile.refresh_activation_receipt() is False


def test_refresh_activation_receipt_returns_false_on_generation_mismatch():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    write_json_durable(
        installation_profile._activation_receipt_path(),
        {**installation_profile._receipt(profile), "generation": "other"},
    )
    assert installation_profile.refresh_activation_receipt() is False


def test_refresh_activation_receipt_returns_false_on_hash_mismatch(monkeypatch):
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    monkeypatch.setattr(installation_profile, "_assert_active_environment", lambda: None)
    write_json_durable(
        installation_profile._activation_receipt_path(),
        {**installation_profile._receipt(profile), "profile_sha256": "deadbeef"},
    )
    assert installation_profile.refresh_activation_receipt() is False


def test_refresh_activation_receipt_rewrites_matching_receipt(monkeypatch):
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    monkeypatch.setattr(installation_profile, "_assert_active_environment", lambda: None)
    write_json_durable(installation_profile._activation_receipt_path(), installation_profile._receipt(profile))
    assert installation_profile.refresh_activation_receipt() is True
    rewritten = json.loads(installation_profile._activation_receipt_path().read_text())
    assert rewritten == installation_profile._receipt(profile)


# --- _bootstrap_ready / selection_pending ---------------------------------

def test_bootstrap_ready_false_when_not_active():
    assert installation_profile._bootstrap_ready({"status": "setup_required"}) is False


def test_bootstrap_ready_false_when_receipt_missing():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    assert installation_profile._bootstrap_ready(profile) is False


def test_bootstrap_ready_false_when_receipt_not_dict():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    installation_profile._activation_receipt_path().write_text("null", encoding="utf-8")
    assert installation_profile._bootstrap_ready(profile) is False


def test_bootstrap_ready_true_when_receipt_matches():
    profile = _active_profile()
    _stage_ready(profile)
    assert installation_profile._bootstrap_ready(profile) is True


def test_selection_pending_states():
    assert installation_profile.selection_pending() is False  # not active
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    assert installation_profile.selection_pending() is True  # active but not ready
    _stage_ready(profile)
    assert installation_profile.selection_pending() is False


# --- capability gates -------------------------------------------------------

def test_allows_rejects_unknown_capability():
    with pytest.raises(installation_profile.InstallationProfileError, match="unknown"):
        installation_profile.allows("bogus")


def test_allows_bootstrap_always_true():
    assert installation_profile.allows(installation_profile.BOOTSTRAP) is True


def test_allows_false_when_not_bootstrap_ready():
    profile = _active_profile()
    installation_profile.stage_activation(profile)
    assert installation_profile.allows(installation_profile.PROVIDER_CONVERSATIONS) is False


def test_allows_provider_conversations_when_ready():
    _stage_ready(_active_profile())
    assert installation_profile.allows(installation_profile.PROVIDER_CONVERSATIONS) is True


def test_allows_toggleable_reads_installation_capabilities(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities,
        "active",
        lambda cap, mode: cap == installation_profile.MOBILE,
    )
    assert installation_profile.allows(installation_profile.MOBILE) is True
    assert installation_profile.allows(installation_profile.INTEGRATIONS) is False


def test_capabilities_reports_setup_required_when_not_ready():
    installation_profile.stage_activation(_active_profile())
    snapshot = installation_profile.capabilities()
    assert snapshot["setup_required"] is True
    assert snapshot["mode"] is None
    assert snapshot["capabilities"] == {}


def test_capabilities_reports_active_detail_when_ready(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities,
        "snapshot",
        lambda mode: {
            installation_profile.MOBILE: {"active": True},
            installation_profile.INTEGRATIONS: {"active": False},
        },
    )
    snapshot = installation_profile.capabilities()
    assert snapshot["status"] == "active"
    assert snapshot["mobile_enabled"] is True
    assert snapshot["integrations_enabled"] is False


def test_set_capability_enabled_records_intent(monkeypatch):
    _stage_ready(_active_profile())
    captured = {}

    def fake_set_enabled(capability, value, mode):
        captured.update(capability=capability, value=value, mode=mode)
        return {capability: value}

    monkeypatch.setattr(installation_capabilities, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(
        installation_capabilities,
        "snapshot",
        lambda mode: {
            installation_profile.MOBILE: {"active": True},
            installation_profile.INTEGRATIONS: {"active": True},
        },
    )
    result = installation_profile.set_capability_enabled(installation_profile.MOBILE, True)
    assert captured == {
        "capability": installation_profile.MOBILE,
        "value": True,
        "mode": installation_profile.DEFAULT,
    }
    assert result["mobile_enabled"] is True


def test_capture_active_capabilities_delegates(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities, "capture_active", lambda mode: {"captured": mode}
    )
    assert installation_profile.capture_active_capabilities() == {"captured": installation_profile.DEFAULT}


def test_capability_requested_false_when_not_ready():
    installation_profile.stage_activation(_active_profile())
    assert installation_profile.capability_requested(installation_profile.MOBILE) is False


def test_capability_requested_reads_settings_when_ready(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities,
        "settings",
        lambda mode: {
            installation_profile.MOBILE: True,
            installation_profile.INTEGRATIONS: False,
        },
    )
    assert installation_profile.capability_requested(installation_profile.MOBILE) is True
    assert installation_profile.capability_requested(installation_profile.INTEGRATIONS) is False


def test_capability_convenience_wrappers(monkeypatch):
    _stage_ready(_active_profile())
    assert installation_profile.provider_conversations_enabled() is True
    monkeypatch.setattr(
        installation_capabilities,
        "active",
        lambda cap, mode: cap == installation_profile.INTEGRATIONS,
    )
    assert installation_profile.integrations_enabled() is True
    assert installation_profile.mobile_enabled() is False


# --- pinned_provider_executable --------------------------------------------

def test_pinned_provider_executable_returns_false_when_not_active():
    assert installation_profile.pinned_provider_executable("claude") == (False, None)


def test_pinned_provider_executable_returns_false_when_command_mismatches():
    _stage_ready(_active_profile(provider="claude"))
    assert installation_profile.pinned_provider_executable("codex") == (False, None)


def test_pinned_provider_executable_returns_false_when_not_ready():
    installation_profile.stage_activation(_active_profile(provider="claude"))
    assert installation_profile.pinned_provider_executable("claude") == (False, None)


def test_pinned_provider_executable_returns_false_when_launcher_gone(tmp_path):
    identity = _identity(command="claude", launcher_path=str(tmp_path / "missing"))
    _stage_ready(_active_profile(provider="claude", identity=identity))
    assert installation_profile.pinned_provider_executable("claude") == (False, None)


def test_pinned_provider_executable_returns_path_when_launcher_present(tmp_path):
    launcher = tmp_path / "claude"
    launcher.write_text("x", encoding="utf-8")
    identity = _identity(command="claude", launcher_path=str(launcher))
    _stage_ready(_active_profile(provider="claude", identity=identity))
    assert installation_profile.pinned_provider_executable("claude") == (True, str(launcher))


# --- repin_provider_executable ---------------------------------------------

def test_repin_returns_false_when_not_active():
    assert installation_profile.repin_provider_executable(_identity()) is False


def test_repin_returns_false_when_provider_is_native():
    installation_profile.stage_activation(
        installation_profile.new_native_active_profile(
            mode=installation_profile.DEFAULT, provider="openai"
        )
    )
    assert installation_profile.repin_provider_executable(_identity(command="openai")) is False


def test_repin_returns_false_on_command_mismatch():
    installation_profile.stage_activation(_active_profile(provider="claude"))
    assert installation_profile.repin_provider_executable(_identity(command="codex")) is False


def test_repin_returns_false_on_launcher_path_mismatch(tmp_path):
    installation_profile.stage_activation(
        _active_profile(provider="claude", identity=_identity(launcher_path="/orig/claude"))
    )
    assert (
        installation_profile.repin_provider_executable(
            _identity(command="claude", launcher_path=str(tmp_path / "claude"))
        )
        is False
    )


def test_repin_returns_false_when_identity_unchanged():
    identity = _identity()
    installation_profile.stage_activation(_active_profile(provider="claude", identity=identity))
    assert installation_profile.repin_provider_executable(identity) is False


def test_repin_without_prior_receipt_updates_profile_only():
    installation_profile.stage_activation(_active_profile(provider="claude"))
    new_identity = _identity(size=2)
    assert installation_profile.repin_provider_executable(new_identity) is True
    assert not installation_profile._activation_receipt_path().exists()
    assert installation_profile.load()["provider_identity"]["size"] == 2


def test_repin_with_prior_receipt_rewrites_receipt():
    profile = _active_profile(provider="claude")
    _stage_ready(profile)
    assert installation_profile.repin_provider_executable(_identity(size=2)) is True
    updated = installation_profile.load()
    receipt = json.loads(installation_profile._activation_receipt_path().read_text())
    assert receipt == installation_profile._receipt(updated)


# --- executable_identity_matches -------------------------------------------

def test_executable_identity_matches_true_on_match(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(provider_setup, "executable_identity", lambda path: identity)
    assert installation_profile.executable_identity_matches(identity) is True


def test_executable_identity_matches_false_on_mismatch(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(
        provider_setup, "executable_identity", lambda path: {**identity, "command": "other"}
    )
    assert installation_profile.executable_identity_matches(identity) is False


def test_executable_identity_matches_false_on_invalid_identity():
    assert installation_profile.executable_identity_matches({"bad": 1}) is False


def test_executable_identity_matches_false_on_oserror(monkeypatch):
    def raise_os(_path):
        raise OSError("boom")

    monkeypatch.setattr(provider_setup, "executable_identity", raise_os)
    assert installation_profile.executable_identity_matches(_identity()) is False


# --- assert_orchestration_mode_allowed -------------------------------------

def test_orchestration_mode_rejects_unsupported():
    with pytest.raises(installation_profile.InstallationProfileError, match="orchestration mode"):
        installation_profile.assert_orchestration_mode_allowed("bogus")


def test_orchestration_mode_rejects_when_conversations_disabled():
    installation_profile.stage_activation(_active_profile())  # not ready
    with pytest.raises(installation_profile.InstallationProfileError, match="setup is required"):
        installation_profile.assert_orchestration_mode_allowed("native")


def test_orchestration_mode_native_allowed_when_ready():
    _stage_ready(_active_profile())
    installation_profile.assert_orchestration_mode_allowed("native")  # no raise


def test_orchestration_mode_manager_normalized_to_team(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities,
        "active",
        lambda cap, mode: cap == installation_profile.INTEGRATIONS,
    )
    installation_profile.assert_orchestration_mode_allowed("manager")  # no raise


def test_orchestration_mode_team_rejects_without_integrations(monkeypatch):
    _stage_ready(_active_profile())
    monkeypatch.setattr(
        installation_capabilities,
        "active",
        lambda cap, mode: cap != installation_profile.INTEGRATIONS,
    )
    with pytest.raises(installation_profile.InstallationProfileError, match="integrations"):
        installation_profile.assert_orchestration_mode_allowed("team")
