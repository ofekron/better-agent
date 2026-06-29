import importlib
import os
import tempfile
from pathlib import Path


def test_file_panel_draft_round_trip_and_delete() -> None:
    old_home = os.environ.get("BETTER_AGENT_HOME")
    old_test_mode = os.environ.get("BETTER_AGENT_TEST_MODE")
    with tempfile.TemporaryDirectory() as td:
        try:
            os.environ["BETTER_AGENT_HOME"] = td
            os.environ["BETTER_AGENT_TEST_MODE"] = "1"

            import file_panel_drafts

            importlib.reload(file_panel_drafts)
            result = file_panel_drafts.write_draft(
                path="/tmp/project/app.ts",
                node_id="primary",
                content="draft",
                base_identity={"mtime_ns": 10, "size": 4},
            )

            assert result["exists"] is True
            assert result["content"] == "draft"
            assert result["base_identity"] == {"mtime_ns": 10, "size": 4}

            loaded = file_panel_drafts.read_draft("/tmp/project/app.ts", "primary")
            assert loaded["exists"] is True
            assert loaded["content"] == "draft"
            assert loaded["base_identity"] == {"mtime_ns": 10, "size": 4}

            draft_files = list((Path(td) / "file-panel-drafts").glob("*.json"))
            assert len(draft_files) == 1

            deleted = file_panel_drafts.delete_draft("/tmp/project/app.ts", "primary")
            assert deleted == {"exists": False}
            assert file_panel_drafts.read_draft("/tmp/project/app.ts", "primary") == {"exists": False}
        finally:
            if old_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = old_home
            if old_test_mode is None:
                os.environ.pop("BETTER_AGENT_TEST_MODE", None)
            else:
                os.environ["BETTER_AGENT_TEST_MODE"] = old_test_mode
