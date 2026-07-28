from __future__ import annotations

import installation_capabilities
import installation_profile
from bundled_extensions import PUBLIC_EXTENSION_PATHS


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
