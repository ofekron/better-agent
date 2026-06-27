"""Locks the per-machine UI-selection store + REST endpoint:
GET/PATCH round-trip, disk persistence, ui_selection_changed broadcast,
and input validation. Runs against an isolated BETTER_AGENT_HOME so it
never touches real session state.
"""

import os
import sys
import tempfile

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ui_sel_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import ui_selection  # noqa: E402


def _client() -> TestClient:
    token = auth.create_token("tester")
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def test_store_roundtrip():
    assert ui_selection.get_all() == {
        "selected_project": None,
        "remembered_session_by_project": {},
    }
    ui_selection.set_selected_project("/repo/a", "primary")
    ui_selection.set_remembered_session("/repo/a", "primary", "sid-1")
    ui_selection.set_remembered_session("/repo/a", "node2", "sid-2")
    snap = ui_selection.get_all()
    assert snap["selected_project"] == {"path": "/repo/a", "node_id": "primary"}
    assert snap["remembered_session_by_project"] == {
        "/repo/a": {"primary": "sid-1", "node2": "sid-2"},
    }
    # Clearing the selected project.
    ui_selection.set_selected_project("")
    assert ui_selection.get_all()["selected_project"] is None
    # Empty path / session id are rejected.
    for bad in (("", "primary", "x"), ("/p", "primary", "")):
        try:
            ui_selection.set_remembered_session(*bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_endpoint_roundtrip():
    captured: list[tuple[str, dict]] = []

    async def _capture(event_type, data):
        captured.append((event_type, data))

    main.coordinator.broadcast_global = _capture  # type: ignore[assignment]

    c = _client()
    r = c.patch(
        "/api/ui-selection",
        json={"selected_project": {"path": "/repo/b", "node_id": "primary"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["selected_project"] == {"path": "/repo/b", "node_id": "primary"}

    r = c.patch(
        "/api/ui-selection",
        json={"remembered_session": {"path": "/repo/b", "node_id": "primary", "session_id": "s9"}},
    )
    assert r.status_code == 200, r.text

    # Persisted across a fresh read (only assert the keys this test wrote;
    # the store-level test shares the same home and seeded other paths).
    snap = c.get("/api/ui-selection").json()
    assert snap["selected_project"] == {"path": "/repo/b", "node_id": "primary"}
    assert snap["remembered_session_by_project"]["/repo/b"] == {"primary": "s9"}

    # Every successful PATCH broadcasts the snapshot.
    assert captured and all(t == "ui_selection_changed" for t, _ in captured)

    # node_id defaults to "primary" when omitted.
    r = c.patch(
        "/api/ui-selection",
        json={"remembered_session": {"path": "/repo/c", "session_id": "s1"}},
    )
    assert r.status_code == 200, r.text
    assert c.get("/api/ui-selection").json()["remembered_session_by_project"]["/repo/c"] == {
        "primary": "s1",
    }

    # Validation rejections → 400.
    for bad in (
        {"remembered_session": {"path": "", "session_id": "s"}},
        {"remembered_session": {"path": "/p", "session_id": ""}},
        {"selected_project": 5},
    ):
        assert c.patch("/api/ui-selection", json=bad).status_code == 400, bad


if __name__ == "__main__":
    test_store_roundtrip()
    test_endpoint_roundtrip()
    print("ui_selection tests passed")
