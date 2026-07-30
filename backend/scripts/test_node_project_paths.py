from __future__ import annotations

import json
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-node-project-paths-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402
import native_mcp_grants  # noqa: E402
import project_mapping_store  # noqa: E402
import project_store  # noqa: E402
import ui_selection  # noqa: E402


WINDOWS = r"C:\Users\Lenovo\better-agent"
MALFORMED_BACKSLASH = (
    r"/Users/ofekron/better-claude/backend/C:\Users\Lenovo\better-agent"
)
MALFORMED_SLASH = "/private/tmp/proof/C:/Users/Lenovo/better-agent"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> None:
    _write_json(
        paths.bc_home() / "projects.json",
        {
            "version": 2,
            "projects": [
                {
                    "path": MALFORMED_BACKSLASH,
                    "node_id": "lenovo",
                    "name": MALFORMED_BACKSLASH,
                    "git_remote": None,
                    "created_at": "2026-01-02T00:00:00",
                    "last_used": "2026-01-03T00:00:00",
                },
                {
                    "path": MALFORMED_SLASH,
                    "node_id": "lenovo",
                    "name": "better-agent",
                    "git_remote": "https://github.com/ofekron/better-agent.git",
                    "created_at": "2026-01-01T00:00:00",
                    "last_used": "2026-01-04T00:00:00",
                },
                {
                    "path": WINDOWS,
                    "node_id": {"invalid": True},
                },
            ],
        },
    )
    _write_json(
        paths.bc_home() / "ui_selection.json",
        {
            "selected_project": {
                "path": MALFORMED_BACKSLASH,
                "node_id": "lenovo",
            },
            "remembered_session_by_project": {
                MALFORMED_SLASH: {"lenovo": "session-1"},
            },
        },
    )
    _write_json(
        paths.bc_home() / "extensions" / "native_mcp_grants.json",
        {
            "schema_version": 1,
            "grants": [{
                "extension_id": "test",
                "server_id": "test",
                "scope": "project",
                "target": f"lenovo\x1f{MALFORMED_BACKSLASH}",
                "digest": "digest",
                "created_at": "2026-01-01T00:00:00",
            }],
        },
    )
    _write_json(
        paths.bc_home() / "project_mappings.json",
        {
            "version": 1,
            "groups": [{
                "group_id": "manual-1",
                "confidence": "manual",
                "label": "Manual",
                "members": [{
                    "node_id": "lenovo",
                    "path": MALFORMED_BACKSLASH,
                    "name": "better-agent",
                }],
            }],
            "rejected_ids": [],
        },
    )

    projects = project_store.list_projects()
    assert len(projects) == 1
    project = projects[0]
    assert project["path"] == WINDOWS
    assert project["name"] == "better-agent"
    assert project["created_at"] == "2026-01-01T00:00:00"
    assert project["last_used"] == "2026-01-04T00:00:00"
    assert project["git_remote"] == "https://github.com/ofekron/better-agent.git"
    assert (paths.bc_home() / "projects.v2.bak.json").exists()

    selection = ui_selection.get_all()
    assert selection["selected_project"] == {
        "path": WINDOWS,
        "node_id": "lenovo",
    }
    assert selection["remembered_session_by_project"] == {
        WINDOWS: {"lenovo": "session-1"},
    }
    mappings = project_mapping_store.list_mappings()
    assert mappings[0]["members"][0]["path"] == WINDOWS
    grants = native_mcp_grants.list_grants()
    assert len(grants) == 1
    assert grants[0].target == f"lenovo\x1f{WINDOWS}"
    quarantine = json.loads(
        (paths.bc_home() / "projects.v2.quarantine.json").read_text(encoding="utf-8")
    )
    assert quarantine["records"][0]["reason"] == "node_id_not_string"
    assert not (paths.bc_home() / "projects.v2.migration.json").exists()

    _write_json(
        paths.bc_home() / "ui_selection.json",
        {
            "selected_project": {
                "path": MALFORMED_BACKSLASH,
                "node_id": "lenovo",
            },
        },
    )
    _write_json(
        paths.bc_home() / "projects.v2.migration.json",
        {
            "rewrites": [{
                "node_id": "lenovo",
                "from": MALFORMED_BACKSLASH,
                "to": WINDOWS,
            }],
        },
    )
    project_store._migrate_legacy_or_raise()
    assert ui_selection.get_selected_project() == {
        "path": WINDOWS,
        "node_id": "lenovo",
    }
    assert not (paths.bc_home() / "projects.v2.migration.json").exists()

    assert project_store.list_projects() == projects
    added = project_store.add_project(
        r"C:/Users/Lenovo/another-project",
        node_id="lenovo",
    )
    assert added
    assert added["path"] == r"C:\Users\Lenovo\another-project"
    assert added["name"] == "another-project"
    assert not added["path"].startswith("/Users/ofekron")


if __name__ == "__main__":
    main()
    print("PASS node project paths stay node-native and v2 repair is resumable")
