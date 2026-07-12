from __future__ import annotations

from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home


_TEST_HOME = _test_home.isolate(prefix="ba-bff-projection-")


def test_detail_overlay_still_applies_draft_fields() -> None:
    import app_chat_draft_store
    import bff_projection

    session_id = "session-detail-1"
    app_chat_draft_store.update(
        session_id, draft_input="hello", client_seq=1, draft_images=[]
    )
    tree = {"id": session_id, "draft_input": "runtime-stale", "forks": []}
    projected = bff_projection.project_json(f"/api/sessions/{session_id}", tree)
    assert projected["draft_input"] == "hello"
    assert projected["draft_input_seq"] == 1
    assert projected["draft_images"] == []


def test_list_overlay_applies_across_all_three_endpoints() -> None:
    import app_chat_draft_store
    import bff_projection

    session_id = "session-list-1"
    app_chat_draft_store.update(
        session_id, draft_input="draft text", client_seq=5, draft_images=[{"id": "img"}]
    )
    for path in ("/api/sessions", "/api/sessions/topbar-pinned", "/api/sessions/summaries"):
        payload = {
            "sessions": [
                {"id": session_id, "name": "s1"},
                {"id": "session-no-draft", "name": "s2"},
            ]
        }
        projected = bff_projection.project_json(path, payload)
        row = next(item for item in projected["sessions"] if item["id"] == session_id)
        assert row["draft_input"] == "draft text", path
        assert row["draft_input_seq"] == 5, path
        assert row["draft_images"] == [{"id": "img"}], path
        other = next(item for item in projected["sessions"] if item["id"] == "session-no-draft")
        assert other["draft_input"] == "", path
        assert other["draft_input_seq"] == 0, path


def test_unmatched_paths_are_not_projected() -> None:
    import bff_projection

    assert bff_projection.needs_json_projection("/api/other") is False
    payload = {"sessions": [{"id": "x"}]}
    projected = bff_projection.project_json("/api/other", payload)
    assert "draft_input" not in projected["sessions"][0]


def test_get_many_edge_cases() -> None:
    import app_chat_draft_store

    assert app_chat_draft_store.get_many([]) == {}

    app_chat_draft_store.update("session-hit", draft_input="present", client_seq=1)
    result = app_chat_draft_store.get_many(["session-hit", "session-miss"])
    assert result["session-hit"]["draft_input"] == "present"
    assert result["session-miss"]["draft_input"] == ""
    assert result["session-miss"]["draft_input_seq"] == 0

    assert app_chat_draft_store.get_many(["not/a valid id"]) == {}


def test_list_overlay_batches_into_a_single_get_many_call() -> None:
    import app_chat_draft_store
    import bff_projection

    calls: list[list[str]] = []
    original = app_chat_draft_store.get_many

    def counting_get_many(session_ids):
        calls.append(list(session_ids))
        return original(session_ids)

    app_chat_draft_store.get_many = counting_get_many
    try:
        payload = {
            "sessions": [{"id": f"session-{i}"} for i in range(10)]
        }
        bff_projection.project_json("/api/sessions", payload)
    finally:
        app_chat_draft_store.get_many = original

    assert len(calls) == 1
    assert set(calls[0]) == {f"session-{i}" for i in range(10)}


if __name__ == "__main__":
    failures = 0
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print(f"PASS {name}")
                except Exception as exc:
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    finally:
        shutil.rmtree(_TEST_HOME)
    sys.exit(1 if failures else 0)
