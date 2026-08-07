import contextlib
import importlib
import json
import os
import tempfile

import pytest


@contextlib.contextmanager
def _drafts_module():
    """Isolate file_panel_drafts against a fresh BETTER_AGENT_HOME tempdir.

    The module resolves its draft root via paths.ba_home() on every call, which
    honors BETTER_AGENT_HOME; reload so any cached import state matches.
    """
    old_home = os.environ.get("BETTER_AGENT_HOME")
    old_test_mode = os.environ.get("BETTER_AGENT_TEST_MODE")
    with tempfile.TemporaryDirectory() as td:
        os.environ["BETTER_AGENT_HOME"] = td
        os.environ["BETTER_AGENT_TEST_MODE"] = "1"
        try:
            import file_panel_drafts

            importlib.reload(file_panel_drafts)
            yield file_panel_drafts
        finally:
            if old_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = old_home
            if old_test_mode is None:
                os.environ.pop("BETTER_AGENT_TEST_MODE", None)
            else:
                os.environ["BETTER_AGENT_TEST_MODE"] = old_test_mode


def test_file_panel_draft_round_trip_and_delete() -> None:
    with _drafts_module() as fpd:
        result = fpd.write_draft(
            path="/tmp/project/app.ts",
            node_id="primary",
            content="draft",
            base_identity={"mtime_ns": 10, "size": 4},
        )

        assert result["exists"] is True
        assert result["content"] == "draft"
        assert result["base_identity"] == {"mtime_ns": 10, "size": 4}

        loaded = fpd.read_draft("/tmp/project/app.ts", "primary")
        assert loaded["exists"] is True
        assert loaded["content"] == "draft"
        assert loaded["base_identity"] == {"mtime_ns": 10, "size": 4}

        deleted = fpd.delete_draft("/tmp/project/app.ts", "primary")
        assert deleted == {"exists": False}
        assert fpd.read_draft("/tmp/project/app.ts", "primary") == {"exists": False}


def test_write_and_read_require_path_and_node_id() -> None:
    with _drafts_module() as fpd:
        with pytest.raises(ValueError, match="path is required"):
            fpd.write_draft(path="", node_id="n", content="x", base_identity=None)
        with pytest.raises(ValueError, match="path is required"):
            fpd.write_draft(path=123, node_id="n", content="x", base_identity=None)
        with pytest.raises(ValueError, match="node_id is required"):
            fpd.write_draft(path="/a", node_id="", content="x", base_identity=None)
        with pytest.raises(ValueError, match="node_id is required"):
            fpd.read_draft("/a", None)


def test_write_draft_requires_string_content() -> None:
    with _drafts_module() as fpd:
        with pytest.raises(ValueError, match="content must be a string"):
            fpd.write_draft(path="/a", node_id="n", content=123, base_identity=None)


def test_delete_missing_draft_is_idempotent() -> None:
    with _drafts_module() as fpd:
        # No draft exists for this key -> delete must not raise and report absent.
        assert fpd.delete_draft("/never/written", "ghost") == {"exists": False}


def test_normalize_identity_rejects_invalid_shapes() -> None:
    with _drafts_module() as fpd:
        assert fpd._normalize_identity(None) is None
        assert fpd._normalize_identity("nope") is None
        # Wrong value types inside an otherwise dict-shaped identity.
        assert fpd._normalize_identity({"mtime_ns": "x", "size": 4}) is None
        assert fpd._normalize_identity({"mtime_ns": 4, "size": None}) is None
        # Well-formed identity survives normalization.
        assert fpd._normalize_identity({"mtime_ns": 9, "size": 3}) == {
            "mtime_ns": 9,
            "size": 3,
        }


def test_read_draft_rejects_path_mismatch() -> None:
    with _drafts_module() as fpd:
        # A draft file exists at this key's location, but its stored path/node_id
        # do not match the read request -> treated as absent.
        target = fpd._draft_path("/a", "n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"path": "/other", "node_id": "n", "content": "x"}),
            encoding="utf-8",
        )
        assert fpd.read_draft("/a", "n") == {"exists": False}


def test_read_draft_rejects_non_string_content() -> None:
    with _drafts_module() as fpd:
        target = fpd._draft_path("/a", "n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"path": "/a", "node_id": "n", "content": 123}),
            encoding="utf-8",
        )
        assert fpd.read_draft("/a", "n") == {"exists": False}
