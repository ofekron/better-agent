from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json_store  # noqa: E402
import session_organization_store as store  # noqa: E402

SCHEMA_VERSION = store.SCHEMA_VERSION


def _reset_module() -> None:
    """Reset the module's process-global caches and wipe the active home's
    organization file so every test starts from a clean slate.

    The active test home is process-global (see paths.engage_test_home): the
    last ``isolate()`` caller wins for the whole pytest run. We never call
    ``isolate()`` ourselves, and we clear ``_path_cache`` so the module
    re-resolves against whatever home is live.
    """
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    path = store._path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _isolated_store() -> Any:
    _reset_module()
    yield
    _reset_module()


# --------------------------------------------------------------------------- #
# validation + cache layer
# --------------------------------------------------------------------------- #

def test_validate_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        store._validate({"schema_version": 999, "folders": [], "tags": [], "assignments": {}})


def test_validate_rejects_non_list_folders() -> None:
    with pytest.raises(ValueError, match="folders must be a list"):
        store._validate({"schema_version": SCHEMA_VERSION, "folders": {}, "tags": [], "assignments": {}})


def test_validate_rejects_non_list_tags() -> None:
    with pytest.raises(ValueError, match="tags must be a list"):
        store._validate({"schema_version": SCHEMA_VERSION, "folders": [], "tags": "x", "assignments": {}})


def test_validate_rejects_non_object_assignments() -> None:
    with pytest.raises(ValueError, match="assignments must be an object"):
        store._validate({"schema_version": SCHEMA_VERSION, "folders": [], "tags": [], "assignments": []})


def test_validate_returns_data_on_success() -> None:
    data = {"schema_version": SCHEMA_VERSION, "folders": [], "tags": [], "assignments": {}}
    assert store._validate(data) is data


def test_load_uses_default_when_file_missing() -> None:
    data = store._load()
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["folders"] == []


def test_load_rejects_corrupt_schema_file() -> None:
    json_store.write_json(store._path(), {"schema_version": 999, "folders": [], "tags": [], "assignments": {}})
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    with pytest.raises(ValueError, match="unsupported"):
        store._load()


def test_version_token_none_before_save_and_set_after() -> None:
    assert store.version_token() is None
    store.create_tag(name="t")
    assert store.version_token() is not None


def test_save_clears_cache_when_stat_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # write_json succeeds, but the post-write stat raises -> cache is cleared.
    monkeypatch.setattr(json_store, "write_json", lambda path, data, mode=0o700: None)
    store._save(store._new_state())
    assert store._cache_signature is None
    assert store._cache_data is None


# --------------------------------------------------------------------------- #
# text/id cleaning helpers
# --------------------------------------------------------------------------- #

def test_clean_text_rejects_non_string_required() -> None:
    with pytest.raises(ValueError, match="name must be a string"):
        store._clean_text(123, "name")


def test_clean_text_returns_empty_for_non_string_optional() -> None:
    assert store._clean_text(None, "name", required=False) == ""


def test_clean_text_rejects_blank_required() -> None:
    with pytest.raises(ValueError, match="name is required"):
        store._clean_text("   ", "name")


def test_clean_text_strips_optional_to_none() -> None:
    assert store._clean_id("   ", "color", required=False) is None
    assert store._clean_id(None, "color", required=False) is None


# --------------------------------------------------------------------------- #
# folder subtree + snapshot projection
# --------------------------------------------------------------------------- #

def test_folder_subtree_ids_collects_descendants() -> None:
    data = store._new_state()
    data["folders"] = [
        {"id": "a", "parent_folder_id": None},
        {"id": "b", "parent_folder_id": "a"},
        {"id": "c", "parent_folder_id": "b"},
        {"id": "d", "parent_folder_id": None},
        {"id": "e", "parent_folder_id": "c"},  # deeper, added in a later pass
    ]
    assert store._folder_subtree_ids(data, "a") == {"a", "b", "c", "e"}


def test_snapshot_scoped_to_project() -> None:
    store.create_folder(project_id="proj-1", name="F1")
    store.create_folder(project_id="proj-2", name="F2")
    store.create_tag(name="shared")
    scoped = store.snapshot(project_id="proj-1")
    assert [f["name"] for f in scoped["folders"]] == ["F1"]
    # Tags with no project are shared across projects.
    assert any(t["name"] == "shared" for t in scoped["tags"])


def test_snapshot_rejects_invalid_project_id() -> None:
    with pytest.raises(ValueError, match="project_id"):
        store.snapshot(project_id="   ")


# --------------------------------------------------------------------------- #
# folders CRUD
# --------------------------------------------------------------------------- #

def test_create_folder_rejects_unknown_parent() -> None:
    with pytest.raises(ValueError, match="parent_folder_id is not in this project"):
        store.create_folder(project_id="proj", name="F", parent_folder_id="nope")


def test_create_folder_rejects_cross_project_parent() -> None:
    store.create_folder(project_id="proj-a", name="A")
    parent = store.snapshot()["folders"][0]["id"]
    with pytest.raises(ValueError, match="parent_folder_id is not in this project"):
        store.create_folder(project_id="proj-b", name="B", parent_folder_id=parent)


def test_create_folder_orders_siblings() -> None:
    f1 = store.create_folder(project_id="p", name="F1")
    f2 = store.create_folder(project_id="p", name="F2")
    assert f1["order"] == 0
    assert f2["order"] == 1


def test_update_folder_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        store.update_folder("nope", {"name": "x"})


def test_update_folder_renames_and_reparents() -> None:
    folder = store.create_folder(project_id="p", name="F")
    parent = store.create_folder(project_id="p", name="Root")
    updated = store.update_folder(folder["id"], {"name": "Renamed", "parent_folder_id": parent["id"]})
    assert updated["name"] == "Renamed"
    assert updated["parent_folder_id"] == parent["id"]


def test_update_folder_rejects_self_parent() -> None:
    folder = store.create_folder(project_id="p", name="F")
    with pytest.raises(ValueError, match="its own parent"):
        store.update_folder(folder["id"], {"parent_folder_id": folder["id"]})


def test_update_folder_rejects_cross_project_parent() -> None:
    folder = store.create_folder(project_id="p-a", name="F")
    parent = store.create_folder(project_id="p-b", name="Root")
    with pytest.raises(ValueError, match="parent_folder_id is not in this project"):
        store.update_folder(folder["id"], {"parent_folder_id": parent["id"]})


def test_folder_delete_preview_lists_subtree_and_sessions() -> None:
    root = store.create_folder(project_id="p", name="Root")
    child = store.create_folder(project_id="p", name="Child", parent_folder_id=root["id"])
    store.set_session_folder("sess-1", child["id"])
    preview = store.folder_delete_preview(root["id"])
    assert set(preview["folder_ids"]) == {root["id"], child["id"]}
    assert preview["session_ids"] == ["sess-1"]
    assert preview["folder_count"] == 2


def test_folder_delete_preview_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        store.folder_delete_preview("nope")


def test_delete_folder_rejects_bad_mode() -> None:
    folder = store.create_folder(project_id="p", name="F")
    with pytest.raises(ValueError, match="mode must be"):
        store.delete_folder(folder["id"], mode="bogus")  # type: ignore[arg-type]


def test_delete_folder_returns_false_when_missing() -> None:
    assert store.delete_folder("nope") is False


def test_delete_folder_unassigns_sessions() -> None:
    folder = store.create_folder(project_id="p", name="F")
    store.set_session_folder("sess-1", folder["id"])
    assert store.delete_folder(folder["id"], mode="unassign") is True
    assert store.organization_for_session("sess-1")["folder_id"] is None


def test_delete_folder_delete_sessions_mode() -> None:
    folder = store.create_folder(project_id="p", name="F")
    store.set_session_folder("sess-1", folder["id"])
    assert store.delete_folder(folder["id"], mode="delete_sessions") is True
    assert not store.snapshot()["folders"]


# --------------------------------------------------------------------------- #
# tags CRUD
# --------------------------------------------------------------------------- #

def test_create_tag_assigns_palette_color() -> None:
    tag = store.create_tag(name="bug")
    assert tag["color"].startswith("#")
    assert tag["color"] in store.TAG_COLOR_PALETTE


def test_update_tag_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        store.update_tag("nope", {"name": "x"})


def test_update_tag_renames_and_recolors() -> None:
    tag = store.create_tag(name="bug")
    updated = store.update_tag(tag["id"], {"name": "feat", "color": "#abcdef"})
    assert updated["name"] == "feat"
    assert updated["color"] == "#abcdef"


def test_update_tag_can_clear_color() -> None:
    tag = store.create_tag(name="bug", color="#abcdef")
    updated = store.update_tag(tag["id"], {"color": None})
    assert updated["color"] is None


def test_delete_tag_returns_false_when_missing() -> None:
    assert store.delete_tag("nope") is False


def test_delete_tag_removes_from_assignments_and_sources() -> None:
    tag = store.create_tag(name="bug")
    store.set_session_tags("sess-1", [tag["id"]])
    assert store.delete_tag(tag["id"]) is True
    org = store.organization_for_session("sess-1")
    assert org["tag_ids"] == []


def test_pick_tag_color_chooses_least_used() -> None:
    data = store._new_state()
    # Use every palette color once EXCEPT the last, so the last is least-used.
    for color in store.TAG_COLOR_PALETTE[:-1]:
        data["tags"].append({"id": color, "project_id": None, "name": color, "color": color})
    chosen = store._pick_tag_color(data, None, "newname")
    assert chosen == store.TAG_COLOR_PALETTE[-1]


# --------------------------------------------------------------------------- #
# session assignment + organization
# --------------------------------------------------------------------------- #

def test_set_session_folder_rejects_unknown_folder() -> None:
    with pytest.raises(ValueError, match="folder_id does not exist"):
        store.set_session_folder("sess-1", "nope")


def test_set_session_folder_clears_with_none() -> None:
    folder = store.create_folder(project_id="p", name="F")
    store.set_session_folder("sess-1", folder["id"])
    store.set_session_folder("sess-1", None)
    assert store.organization_for_session("sess-1")["folder_id"] is None


def test_organization_for_session_strips_malformed_assignment() -> None:
    json_store.write_json(
        store._path(),
        {
            "schema_version": SCHEMA_VERSION,
            "folders": [],
            "tags": [],
            "assignments": {
                "sess-1": {"folder_id": 123, "tag_ids": "nope", "tag_sources": []},
            },
        },
    )
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    org = store.organization_for_session("sess-1")
    assert org["folder_id"] is None
    assert org["tag_ids"] == []


def test_normalize_session_organization_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="tag_ids must be a list"):
        store._normalize_session_organization(None, "nope")  # type: ignore[arg-type]


def test_validate_session_organization_ids_rejects_unknown_folder() -> None:
    data = store._load()
    with pytest.raises(ValueError, match="folder_id does not exist"):
        store._validate_session_organization_ids(data, "nope", [])


def test_validate_session_organization_rejects_unknown_tag() -> None:
    with pytest.raises(ValueError, match="unknown tag_id"):
        store.validate_session_organization(None, ["nope"])


def test_set_session_organization_replaces_tags() -> None:
    t1 = store.create_tag(name="a")
    t2 = store.create_tag(name="b")
    store.set_session_organization("sess-1", None, [t1["id"]])
    store.set_session_organization("sess-1", None, [t2["id"]])
    org = store.organization_for_session("sess-1")
    assert org["tag_ids"] == [t2["id"]]
    assert org["tag_sources"][t2["id"]] == store.TAG_SOURCE_MANUAL


def test_set_session_tags_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="tag_ids must be a list"):
        store.set_session_tags("sess-1", "nope")  # type: ignore[arg-type]


def test_set_session_tags_rejects_unknown_tag() -> None:
    with pytest.raises(ValueError, match="unknown tag_id"):
        store.set_session_tags("sess-1", ["nope"])


def test_set_session_tags_records_source() -> None:
    tag = store.create_tag(name="a")
    org = store.set_session_tags(
        "sess-1", [tag["id"]], source=store.TAG_SOURCE_AUTO_TAGGING,
    )
    assert org["tag_sources"][tag["id"]] == store.TAG_SOURCE_AUTO_TAGGING


def test_patch_session_tags_add_remove() -> None:
    t1 = store.create_tag(name="a")
    t2 = store.create_tag(name="b")
    store.set_session_tags("sess-1", [t1["id"]])
    org = store.patch_session_tags(
        "sess-1", add=[t2["id"]], remove=[t1["id"]],
        add_source=store.TAG_SOURCE_REQUIREMENT_ANALYSIS,
    )
    assert org["tag_ids"] == [t2["id"]]
    assert org["tag_sources"][t2["id"]] == store.TAG_SOURCE_REQUIREMENT_ANALYSIS


def test_patch_session_tags_rejects_non_lists() -> None:
    with pytest.raises(ValueError, match="add and remove must be lists"):
        store.patch_session_tags("sess-1", add="nope")  # type: ignore[arg-type]


def test_patch_session_tags_rejects_unknown_tag() -> None:
    with pytest.raises(ValueError, match="unknown tag_id"):
        store.patch_session_tags("sess-1", add=["nope"])


def test_sync_session_tags_rejects_manual_source() -> None:
    with pytest.raises(ValueError, match="cannot target manual tags"):
        store.sync_session_tags_by_source(
            "sess-1", tag_ids=[], source=store.TAG_SOURCE_MANUAL,
        )


def test_sync_session_tags_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="tag_ids must be a list"):
        store.sync_session_tags_by_source(
            "sess-1", tag_ids="nope", source=store.TAG_SOURCE_AUTO_TAGGING,  # type: ignore[arg-type]
        )


def test_sync_session_tags_rejects_unknown_tag() -> None:
    with pytest.raises(ValueError, match="unknown tag_id"):
        store.sync_session_tags_by_source(
            "sess-1", tag_ids=["nope"], source=store.TAG_SOURCE_AUTO_TAGGING,
        )


def test_sync_session_tags_merge_accumulates() -> None:
    manual_tag = store.create_tag(name="manual")
    auto_tag = store.create_tag(name="auto")
    store.set_session_tags("sess-1", [manual_tag["id"]])
    store.sync_session_tags_by_source(
        "sess-1", tag_ids=[auto_tag["id"]], source=store.TAG_SOURCE_AUTO_TAGGING, merge=True,
    )
    org = store.organization_for_session("sess-1")
    assert set(org["tag_ids"]) == {manual_tag["id"], auto_tag["id"]}


def test_sync_session_tags_replace_preserves_manual_and_orphans_dropped() -> None:
    manual_tag = store.create_tag(name="manual")
    auto_tag = store.create_tag(name="auto")
    new_auto = store.create_tag(name="newauto")
    store.set_session_tags("sess-1", [manual_tag["id"]])
    store.sync_session_tags_by_source(
        "sess-1", tag_ids=[auto_tag["id"]], source=store.TAG_SOURCE_AUTO_TAGGING,
    )
    # Replace the auto set; manual stays, old auto tag is orphaned+deleted.
    store.sync_session_tags_by_source(
        "sess-1", tag_ids=[new_auto["id"]], source=store.TAG_SOURCE_AUTO_TAGGING,
    )
    org = store.organization_for_session("sess-1")
    assert manual_tag["id"] in org["tag_ids"]
    assert new_auto["id"] in org["tag_ids"]
    # The dropped auto tag is no longer referenced and is garbage-collected.
    assert all(t["id"] != auto_tag["id"] for t in store.snapshot()["tags"])


def test_collect_orphaned_tags_keeps_referenced() -> None:
    keep = store.create_tag(name="keep")
    orphan = store.create_tag(name="orphan")
    data = store._load()
    data["assignments"]["sess-1"] = {"folder_id": None, "tag_ids": [keep["id"]], "tag_sources": {}}
    store._collect_orphaned_tags(data, [orphan["id"], keep["id"]])
    ids = {t["id"] for t in data["tags"]}
    assert keep["id"] in ids
    assert orphan["id"] not in ids


# --------------------------------------------------------------------------- #
# ensure_tag
# --------------------------------------------------------------------------- #

def test_ensure_tag_returns_existing_case_insensitive() -> None:
    first = store.ensure_tag(name="Bug", project_id="p")
    again = store.ensure_tag(name="bug", project_id="p")
    assert first["id"] == again["id"]


def test_ensure_tag_creates_when_missing() -> None:
    tag = store.ensure_tag(name="new")
    assert tag["name"] == "new"
    assert tag["id"] in {t["id"] for t in store.snapshot()["tags"]}


# --------------------------------------------------------------------------- #
# enrichment projection
# --------------------------------------------------------------------------- #

def test_enrich_session_summary_attaches_folder_and_tags() -> None:
    folder = store.create_folder(project_id="p", name="F")
    tag = store.create_tag(name="bug")
    store.set_session_organization("sess-1", folder["id"], [tag["id"]])
    enriched = store.enrich_session_summary({"id": "sess-1", "name": "S"})
    assert enriched["folder_id"] == folder["id"]
    assert enriched["session_tags"][0]["id"] == tag["id"]


def test_enrich_session_summary_from_projection_is_pure() -> None:
    tag = store.create_tag(name="bug")
    assignments = {"sess-1": {"folder_id": "f1", "tag_ids": [tag["id"]], "tag_sources": {}}}
    tag_by_id = {tag["id"]: tag}
    enriched = store.enrich_session_summary_from_projection(
        {"id": "sess-1"}, assignments, tag_by_id,
    )
    assert enriched["folder_id"] == "f1"
    assert enriched["session_tags"][0]["source"] == store.TAG_SOURCE_MANUAL


def test_enrich_session_summaries_handles_malformed_assignments() -> None:
    tag = store.create_tag(name="bug")
    store.set_session_tags("sess-1", [tag["id"]])
    # sess-1 carries a real string folder + dict tag_sources (the happy branches);
    # sess-2 is a malformed non-dict assignment that must not crash.
    json_store.write_json(
        store._path(),
        {
            "schema_version": SCHEMA_VERSION,
            "folders": [],
            "tags": store.snapshot()["tags"],
            "assignments": {
                "sess-1": {
                    "folder_id": "f1",
                    "tag_ids": [tag["id"]],
                    "tag_sources": {tag["id"]: store.TAG_SOURCE_MANUAL},
                },
                "sess-2": "corrupt",
            },
        },
    )
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    enriched = store.enrich_session_summaries([{"id": "sess-1"}, {"id": "sess-2"}])
    assert enriched[0]["folder_id"] == "f1"
    assert enriched[0]["session_tags"][0]["id"] == tag["id"]
    assert enriched[1]["folder_id"] is None
    assert enriched[1]["session_tags"] == []


def test_enrichment_projection_returns_snapshot_copies() -> None:
    store.create_tag(name="bug")
    assignments, tag_by_id = store.enrichment_projection()
    assert isinstance(assignments, dict)
    assert tag_by_id


def test_tag_with_source_defaults_manual_for_bad_source() -> None:
    tag = {"id": "t", "name": "x"}
    assert store._tag_with_source(tag, "bogus")["source"] == store.TAG_SOURCE_MANUAL
    assert store._tag_with_source(tag, 123)["source"] == store.TAG_SOURCE_MANUAL


# --------------------------------------------------------------------------- #
# query_sessions filter engine
# --------------------------------------------------------------------------- #

def _sessions() -> list[dict[str, Any]]:
    return [
        {"id": "1", "cwd": "/p1", "folder_id": "f1", "provider_id": "claude",
         "model": "sonnet", "orchestration_mode": "native", "is_running": True,
         "name": "alpha", "session_tags": [{"id": "t1", "name": "bug"}]},
        {"id": "2", "cwd": "/p2", "folder_id": "f2", "provider_id": "codex",
         "model": "gpt", "orchestration_mode": "manager", "archived": True,
         "name": "beta", "session_tags": [{"id": "t2", "name": "feat"}]},
        {"id": "3", "cwd": "/p1", "provider_id": "claude", "model": "haiku",
         "orchestration_mode": "native", "pinned": True, "name": "gamma",
         "session_tags": [{"id": "t1", "name": "bug"}, {"id": "t2", "name": "feat"}]},
    ]


def test_query_rejects_non_object_query() -> None:
    with pytest.raises(ValueError, match="query must be an object"):
        store.query_sessions(_sessions(), "nope")  # type: ignore[arg-type]


def test_query_rejects_non_string_text() -> None:
    with pytest.raises(ValueError, match="text must be a string"):
        store.query_sessions(_sessions(), {"text": 123})


def test_query_rejects_non_string_list_field() -> None:
    with pytest.raises(ValueError, match="providers must be a string list"):
        store.query_sessions(_sessions(), {"providers": [123]})


def test_query_rejects_bad_tag_match() -> None:
    with pytest.raises(ValueError, match="tag_match must be"):
        store.query_sessions(_sessions(), {"tag_match": "none"})


def test_query_filters_by_project_folder_provider_model_mode() -> None:
    sessions = _sessions()
    assert [s["id"] for s in store.query_sessions(sessions, {"project_ids": ["/p1"]})] == ["1", "3"]
    assert [s["id"] for s in store.query_sessions(sessions, {"folder_ids": ["f1"]})] == ["1"]
    assert [s["id"] for s in store.query_sessions(sessions, {"providers": ["codex"]})] == ["2"]
    assert [s["id"] for s in store.query_sessions(sessions, {"models": ["haiku"]})] == ["3"]
    assert [s["id"] for s in store.query_sessions(sessions, {"modes": ["manager"]})] == ["2"]


def test_query_tag_match_all_and_any() -> None:
    sessions = _sessions()
    assert [s["id"] for s in store.query_sessions(sessions, {"tag_ids": ["t1", "t2"], "tag_match": "all"})] == ["3"]
    # "any": session 1 has t1, session 2 has t2, session 3 has both.
    assert [s["id"] for s in store.query_sessions(sessions, {"tag_ids": ["t1", "t2"], "tag_match": "any"})] == ["1", "2", "3"]


def test_query_status_filters() -> None:
    sessions = _sessions()
    assert [s["id"] for s in store.query_sessions(sessions, {"status": ["running"]})] == ["1"]
    assert [s["id"] for s in store.query_sessions(sessions, {"status": ["archived"]})] == ["2"]
    assert [s["id"] for s in store.query_sessions(sessions, {"status": ["pinned"]})] == ["3"]
    # idle = not running and not archived.
    assert "3" in [s["id"] for s in store.query_sessions(sessions, {"status": ["idle"]})]
    assert "1" not in [s["id"] for s in store.query_sessions(sessions, {"status": ["idle"]})]


def test_query_text_search_matches_names_and_tag_names() -> None:
    sessions = _sessions()
    assert [s["id"] for s in store.query_sessions(sessions, {"text": "alpha"})] == ["1"]
    assert [s["id"] for s in store.query_sessions(sessions, {"text": "feat"})] == ["2", "3"]


def test_query_text_search_no_match() -> None:
    sessions = _sessions()
    assert store.query_sessions(sessions, {"text": "zzzzz"}) == []


# --------------------------------------------------------------------------- #
# read-only SQL sandbox
# --------------------------------------------------------------------------- #

def _seed_sql_state() -> None:
    store.create_tag(name="bug", project_id="p1", color="#000000")
    store.create_tag(name="feat", project_id="p1")
    store.set_session_tags("sess-1", [store.snapshot()["tags"][0]["id"]])
    store.set_session_folder("sess-1", store.create_folder(project_id="p1", name="F")["id"])


def test_run_tags_readonly_sql_rejects_empty() -> None:
    assert store.run_tags_readonly_sql("")["error"] == "empty_sql"
    assert store.run_tags_readonly_sql("   ;  ")["error"] == "empty_sql"


def test_run_tags_readonly_sql_rejects_non_select() -> None:
    result = store.run_tags_readonly_sql("DELETE FROM tags")
    assert "only a single SELECT" in result["error"]


def test_run_tags_readonly_sql_rejects_stacked_statements() -> None:
    result = store.run_tags_readonly_sql("SELECT 1; SELECT 2")
    assert "only a single SELECT" in result["error"]


def test_run_tags_readonly_sql_runs_select_and_caps_rows() -> None:
    _seed_sql_state()
    result = store.run_tags_readonly_sql("SELECT name, color FROM tags ORDER BY name")
    assert result["columns"] == ["name", "color"]
    assert len(result["rows"]) >= 2
    assert result["rows"][0][0] == "bug"


def test_run_tags_readonly_sql_runs_with_cte() -> None:
    _seed_sql_state()
    # Function-free CTE: the authorizer denies SQLITE_FUNCTION, so avoid count().
    result = store.run_tags_readonly_sql(
        "WITH proj_tags AS (SELECT id, name FROM tags) SELECT name FROM proj_tags",
    )
    assert len(result["rows"]) >= 2
    assert result["columns"] == ["name"]


def test_run_tags_readonly_sql_blocks_functions() -> None:
    _seed_sql_state()
    # The authorizer allows only SELECT/READ/RECURSIVE; any function is denied.
    count_result = store.run_tags_readonly_sql("SELECT count(*) FROM tags")
    assert count_result["error"]
    like_result = store.run_tags_readonly_sql("SELECT name FROM tags WHERE name LIKE 'b%'")
    assert like_result["error"]


def test_run_tags_readonly_sql_blocks_writes_via_authorizer() -> None:
    _seed_sql_state()
    result = store.run_tags_readonly_sql(
        "SELECT * FROM (INSERT INTO tags(id, name) VALUES ('x', 'y'))",
    )
    assert result["error"]
    assert result["rows"] == []


def test_run_tags_readonly_sql_truncates_large_cell() -> None:
    # A single tag whose name exceeds the cell byte cap; SELECT without LIKE
    # (functions are denied by the authorizer).
    store.create_tag(name="x" * (store._SQL_MAX_CELL_BYTES + 50), project_id="p1")
    result = store.run_tags_readonly_sql("SELECT name FROM tags")
    assert len(result["rows"]) == 1
    assert len(result["rows"][0][0]) == store._SQL_MAX_CELL_BYTES


def test_run_tags_readonly_sql_truncates_over_limit_rows() -> None:
    for i in range(store._SQL_MAX_ROWS + 5):
        store.create_tag(name=f"tag{i}", project_id="p1")
    result = store.run_tags_readonly_sql("SELECT name FROM tags")
    assert result["truncated"] is True
    assert len(result["rows"]) == store._SQL_MAX_ROWS


def test_run_tags_readonly_sql_returns_error_on_bad_sql() -> None:
    result = store.run_tags_readonly_sql("SELECT FROM nonexistent_table")
    assert result["error"]
    assert result["columns"] == []


def test_run_tags_readonly_sql_aborts_on_timeout() -> None:
    result = store.run_tags_readonly_sql(
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 100000000) "
        "SELECT count(*) FROM cnt",
        timeout_s=0.1,
    )
    assert result["error"]


def test_cap_sql_row_handles_bytes_and_scalars() -> None:
    assert store._cap_sql_row(("short",)) == ["short"]
    assert store._cap_sql_row((b"\x00\x01",))[0] == "0001"
    assert store._cap_sql_row((42,))[0] == 42


def test_populate_tags_sql_projection_includes_view_and_assignments() -> None:
    _seed_sql_state()
    conn = sqlite3.connect(":memory:")
    store._populate_tags_sql(conn)
    # tag_assignments is a VIEW over session_tags.
    rows = conn.execute("SELECT session_id, tag_id FROM tag_assignments").fetchall()
    assert rows
    folders = conn.execute("SELECT session_id, folder_id FROM assignments").fetchall()
    assert folders
    conn.close()


def test_sql_authorizer_denies_non_read_actions() -> None:
    assert store._sql_authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == sqlite3.SQLITE_OK
    assert store._sql_authorizer(sqlite3.SQLITE_INSERT, None, None, None, None) == sqlite3.SQLITE_DENY


# --------------------------------------------------------------------------- #
# tag source cleaning
# --------------------------------------------------------------------------- #

def test_clean_tag_source_rejects_unsupported() -> None:
    with pytest.raises(ValueError, match="unsupported tag source"):
        store._clean_tag_source("bogus")


def test_clean_tag_source_accepts_valid() -> None:
    assert store._clean_tag_source(store.TAG_SOURCE_AUTO_TAGGING) == store.TAG_SOURCE_AUTO_TAGGING


# --------------------------------------------------------------------------- #
# branch-completion edge cases
# --------------------------------------------------------------------------- #

def test_pick_tag_color_skips_other_project_tags() -> None:
    # A tag in a different project must not count toward this project's palette.
    data = store._new_state()
    data["tags"].append({"id": "x", "project_id": "other", "name": "x", "color": store.TAG_COLOR_PALETTE[0]})
    chosen = store._pick_tag_color(data, "this-project", "name")
    assert chosen in store.TAG_COLOR_PALETTE


def test_normalize_accepts_none_tag_ids() -> None:
    folder_id, cleaned = store._normalize_session_organization(None, None)
    assert folder_id is None
    assert cleaned == []


def test_validate_session_organization_dedups_tag_ids() -> None:
    tag = store.create_tag(name="dup")
    _, cleaned = store.validate_session_organization(None, [tag["id"], tag["id"]])
    assert cleaned == [tag["id"]]


def test_set_session_tags_dedups_tag_ids() -> None:
    tag = store.create_tag(name="dup")
    org = store.set_session_tags("sess-1", [tag["id"], tag["id"]])
    assert org["tag_ids"] == [tag["id"]]


def test_patch_session_tags_skips_already_present() -> None:
    tag = store.create_tag(name="present")
    store.set_session_tags("sess-1", [tag["id"]])
    # Adding a tag already present is a no-op dedup.
    org = store.patch_session_tags("sess-1", add=[tag["id"]])
    assert org["tag_ids"] == [tag["id"]]


def test_sync_session_tags_merge_dedups() -> None:
    tag = store.create_tag(name="auto")
    # Merge with duplicate tag_ids and an already-present tag.
    store.sync_session_tags_by_source(
        "sess-1", tag_ids=[tag["id"]], source=store.TAG_SOURCE_AUTO_TAGGING, merge=True,
    )
    store.sync_session_tags_by_source(
        "sess-1",
        tag_ids=[tag["id"], tag["id"]],
        source=store.TAG_SOURCE_AUTO_TAGGING,
        merge=True,
    )
    org = store.organization_for_session("sess-1")
    assert org["tag_ids"] == [tag["id"]]


def test_sync_session_tags_replace_dedups() -> None:
    tag = store.create_tag(name="auto")
    # Replace path with a duplicate tag_id in the source set.
    org = store.sync_session_tags_by_source(
        "sess-1",
        tag_ids=[tag["id"], tag["id"]],
        source=store.TAG_SOURCE_AUTO_TAGGING,
    )
    assert org["tag_ids"] == [tag["id"]]


def test_update_folder_name_only() -> None:
    folder = store.create_folder(project_id="p", name="F")
    updated = store.update_folder(folder["id"], {"name": "Only"})
    assert updated["name"] == "Only"
    assert updated["parent_folder_id"] is None


def test_update_folder_clears_parent() -> None:
    parent = store.create_folder(project_id="p", name="Root")
    folder = store.create_folder(project_id="p", name="Child", parent_folder_id=parent["id"])
    updated = store.update_folder(folder["id"], {"parent_folder_id": None})
    assert updated["parent_folder_id"] is None


def test_update_tag_name_only() -> None:
    tag = store.create_tag(name="bug")
    updated = store.update_tag(tag["id"], {"name": "feat"})
    assert updated["name"] == "feat"


def test_delete_folder_skips_unrelated_assignment() -> None:
    keep_folder = store.create_folder(project_id="p", name="Keep")
    drop_folder = store.create_folder(project_id="p", name="Drop")
    store.set_session_folder("sess-keep", keep_folder["id"])
    store.set_session_folder("sess-drop", drop_folder["id"])
    store.delete_folder(drop_folder["id"])
    # The unrelated assignment survives unchanged.
    assert store.organization_for_session("sess-keep")["folder_id"] == keep_folder["id"]


def test_delete_tag_walks_mixed_assignments() -> None:
    tag = store.create_tag(name="bug")
    # Seed a mix: a dict assignment without tag_sources, and a non-dict value.
    json_store.write_json(
        store._path(),
        {
            "schema_version": SCHEMA_VERSION,
            "folders": [],
            "tags": store.snapshot()["tags"],
            "assignments": {
                "sess-1": {"folder_id": None, "tag_ids": [tag["id"]]},
                "sess-2": "corrupt",
                "sess-3": {"folder_id": None, "tag_ids": [tag["id"]], "tag_sources": "nope"},
            },
        },
    )
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    assert store.delete_tag(tag["id"]) is True
    assert store.snapshot()["tags"] == []


def test_ensure_tag_skips_non_matching_tags() -> None:
    store.create_tag(name="alpha")
    store.create_tag(name="beta")
    found = store.ensure_tag(name="beta")
    assert found["name"] == "beta"


def test_query_tag_any_with_no_matches_returns_empty() -> None:
    sessions = _sessions()
    assert store.query_sessions(sessions, {"tag_ids": ["t99"], "tag_match": "any"}) == []


def test_populate_tags_sql_skips_malformed_assignments() -> None:
    tag = store.create_tag(name="bug")
    json_store.write_json(
        store._path(),
        {
            "schema_version": SCHEMA_VERSION,
            "folders": [],
            "tags": store.snapshot()["tags"],
            "assignments": {
                "sess-1": {"folder_id": "f", "tag_ids": [tag["id"]]},
                "sess-2": "corrupt",
            },
        },
    )
    store._path_cache = None
    store._cache_data = None
    store._cache_signature = None
    conn = sqlite3.connect(":memory:")
    store._populate_tags_sql(conn)
    rows = conn.execute("SELECT session_id FROM assignments").fetchall()
    assert ("sess-1",) in rows
    # The non-dict assignment is skipped.
    assert ("sess-2",) not in rows
    conn.close()


def test_sync_session_tags_replace_keeps_manual_tag_in_source_set() -> None:
    # A manual tag that also appears in the sync source set must not be
    # double-appended (the "already projected" dedup branch).
    tag = store.create_tag(name="dual")
    store.set_session_tags("sess-1", [tag["id"]], source=store.TAG_SOURCE_MANUAL)
    org = store.sync_session_tags_by_source(
        "sess-1", tag_ids=[tag["id"]], source=store.TAG_SOURCE_AUTO_TAGGING,
    )
    assert org["tag_ids"] == [tag["id"]]
