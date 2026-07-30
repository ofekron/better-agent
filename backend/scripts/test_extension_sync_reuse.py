from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import extension_store


def test_unchanged_synced_artifact_reuses_completed_node_package(
    tmp_path: Path,
) -> None:
    extension_id = "example.cached"
    archive = b"content-addressed artifact"
    artifact_sha = hashlib.sha256(archive).hexdigest()
    target = tmp_path / extension_id / "versions" / artifact_sha
    target.mkdir(parents=True)
    (target / "better-agent-extension.json").write_text("{}", encoding="utf-8")
    extension_store._write_package_completion(target)
    records = {
        extension_id: {
            "manifest": {"id": extension_id},
            "source": {},
        },
    }
    current_records = {
        extension_id: {
            "manifest": {"id": extension_id},
            "smoke_test": {"status": "passed", "checked_at": "node"},
            "source": {
                "synced_artifact_sha256": artifact_sha,
                "install_path": str(target),
            },
        },
    }
    artifact = {
        "extension_id": extension_id,
        "archive_b64": base64.b64encode(archive).decode("ascii"),
        "artifact_sha256": artifact_sha,
    }

    with (
        patch.object(extension_store, "_install_root", return_value=tmp_path),
        patch.object(
            extension_store,
            "validate_manifest",
            return_value={"id": extension_id},
        ),
        patch.object(extension_store, "_validate_declared_files"),
        patch.object(
            extension_store,
            "_install_python_requirements",
            Mock(),
        ) as install_requirements,
        patch.object(
            extension_store,
            "_run_extension_smoke_test",
            Mock(),
        ) as smoke,
    ):
        extension_store._install_synced_artifact(
            artifact,
            records,
            current_records,
        )

    install_requirements.assert_not_called()
    smoke.assert_not_called()
    assert records[extension_id]["source"]["install_path"] == str(target)
    assert records[extension_id]["smoke_test"]["checked_at"] == "node"


def _assert_unverified_synced_artifact_runs_full_node_smoke(
    tmp_path: Path,
    *,
    current_sha: str,
    tamper: bool,
) -> None:
    extension_id = "example.changed"
    archive = b"changed artifact"
    artifact_sha = hashlib.sha256(archive).hexdigest()
    target = tmp_path / extension_id / "versions" / artifact_sha
    target.mkdir(parents=True)
    manifest_path = target / "better-agent-extension.json"
    manifest_path.write_text("{}", encoding="utf-8")
    extension_store._write_package_completion(target)
    if tamper:
        manifest_path.write_text('{"tampered":true}', encoding="utf-8")
    records = {extension_id: {"manifest": {"id": extension_id}, "source": {}}}
    current_records = {
        extension_id: {
            "source": {"synced_artifact_sha256": current_sha},
        },
    }
    artifact = {
        "extension_id": extension_id,
        "archive_b64": base64.b64encode(archive).decode("ascii"),
        "artifact_sha256": artifact_sha,
    }

    def extract(_archive: bytes, staging: Path) -> None:
        staging.mkdir(parents=True)
        (staging / "better-agent-extension.json").write_text(
            "{}",
            encoding="utf-8",
        )

    with (
        patch.object(extension_store, "_install_root", return_value=tmp_path),
        patch.object(extension_store, "_safe_extract_tar_gz", side_effect=extract),
        patch.object(
            extension_store,
            "validate_manifest",
            return_value={"id": extension_id},
        ),
        patch.object(extension_store, "_validate_declared_files"),
        patch.object(extension_store, "_install_python_requirements"),
        patch.object(
            extension_store,
            "_run_extension_smoke_test",
            return_value={"status": "passed"},
        ) as smoke,
        patch.object(extension_store, "_write_package_completion"),
        patch.object(extension_store, "_publish_package_dir"),
    ):
        extension_store._install_synced_artifact(
            artifact,
            records,
            current_records,
        )

    smoke.assert_called_once()


def test_tampered_synced_artifact_runs_full_node_smoke(tmp_path: Path) -> None:
    archive_sha = hashlib.sha256(b"changed artifact").hexdigest()
    _assert_unverified_synced_artifact_runs_full_node_smoke(
        tmp_path,
        current_sha=archive_sha,
        tamper=True,
    )


def test_mismatched_node_record_runs_full_node_smoke(tmp_path: Path) -> None:
    _assert_unverified_synced_artifact_runs_full_node_smoke(
        tmp_path,
        current_sha="0" * 64,
        tamper=False,
    )
