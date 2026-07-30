from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import extension_store


def test_removed_provider_config_sync_cannot_survive_store_reconciliation(
    tmp_path: Path,
) -> None:
    extension_id = "ofek-dev.provider-config-sync"
    package = tmp_path / extension_id
    (package / "versions" / "stale").mkdir(parents=True)
    data = {
        "extensions": {
            extension_id: {
                "enabled": True,
                "manifest": {
                    "id": extension_id,
                    "entrypoints": {
                        "mcp": [{
                            "replaces_builtin": "provider-config-sync",
                        }],
                    },
                },
            },
        },
    }

    with patch.object(extension_store, "_install_root", return_value=tmp_path):
        changed = extension_store._purge_obsolete_extension_records(data)
        extension_store._rehydrate_installed_extension_records(data)

    assert changed is True
    assert extension_id not in data["extensions"]
    assert not package.exists()


def test_removed_provider_config_sync_cannot_rehydrate_when_cleanup_fails(
    tmp_path: Path,
) -> None:
    extension_id = "ofek-dev.provider-config-sync"
    package = tmp_path / extension_id
    version = package / "versions" / "stale"
    version.mkdir(parents=True)
    (version / "better-agent-extension.json").write_text("{}", encoding="utf-8")
    data = {"extensions": {extension_id: {"enabled": True}}}

    with (
        patch.object(extension_store, "_install_root", return_value=tmp_path),
        patch.object(extension_store.shutil, "rmtree", side_effect=OSError("busy")) as remove,
        patch.object(
            extension_store,
            "validate_manifest",
            return_value={"id": extension_id},
        ),
    ):
        extension_store._purge_obsolete_extension_records(data)
        extension_store._rehydrate_installed_extension_records(data)
        extension_store._rehydrate_installed_extension_records(data)

    assert remove.call_count == 2
    assert package.exists()
    assert extension_id not in data["extensions"]


def test_removed_provider_config_sync_is_excluded_from_sync_projection() -> None:
    extension_id = "ofek-dev.provider-config-sync"
    data = {
        "extensions": {
            extension_id: {
                "manifest": {"id": extension_id},
                "source": {},
            },
        },
        "deleted_extensions": {},
    }

    with (
        patch.object(extension_store, "_load_with_changes", return_value=(data, False, False)),
        patch.object(extension_store, "_load_ext_settings", return_value={}),
        patch.object(extension_store, "_load_ui_settings", return_value={}),
    ):
        projection = extension_store.export_extension_sync_state()

    assert extension_id not in projection["store"]["extensions"]
    assert all(
        artifact["extension_id"] != extension_id
        for artifact in projection["artifacts"]
    )


def test_removed_provider_config_sync_cannot_be_seeded() -> None:
    extension_id = "ofek-dev.provider-config-sync"
    data = {
        "extensions": {
            extension_id: {
                "manifest": {"id": extension_id},
                "source": {"type": "local", "package_path": "/does/not/matter"},
            },
        },
        "deleted_extensions": {},
    }

    local_changed, _recovered = extension_store._ensure_local_extensions(data)

    assert local_changed is True
    assert extension_id not in data["extensions"]

    with (
        patch.object(
            extension_store,
            "_PUBLIC_EXTENSION_PATHS",
            {extension_id: "extensions/provider-config-sync"},
        ),
        patch.object(extension_store, "_repo_root", return_value=Path("/repo")),
        patch.object(extension_store, "_required_marketplace_repo_root", return_value=None),
        patch.object(extension_store, "_install_public_package_snapshot", Mock()) as install,
    ):
        assert extension_store._ensure_public_extensions(data) is False

    install.assert_not_called()
