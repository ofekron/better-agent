from __future__ import annotations

import installation_profile


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
