"""Harness-profile session assignment REST endpoint.

Assigning a profile to a session, and clearing it back to the default, must
persist on the session record and be reflected by the resolver — both
through the REST response and via the direct session-store read.
"""
from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_test_home.isolate("ba-session-harness-profile-api-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Only the bundled-extension activation gate decides profile applicability in
# an isolated test home — not a fake install.
installation_profile.integrations_enabled = lambda: True
installation_profile.allows = lambda _capability: True


def test_harness_profile_assignment_persists_and_resolves() -> None:
    capture_profile = harness_profile_store.create_profile({
        "id": "model-traffic-test",
        "name": "Model Traffic Test",
    })
    session = session_manager.create(
        name="profile-assignment",
        orchestration_mode="native",
        model="model-x",
    )
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})

    assigned = client.patch(
        f"/api/sessions/{session['id']}/harness-profile",
        json={"harness_profile_id": capture_profile["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    assigned_session = session_manager.get(session["id"])
    assert assigned.json()["harness_profile_id"] == capture_profile["id"]
    assert assigned_session["harness_profile_id"] == capture_profile["id"]
    assert (
        harness_profile_resolver.resolve_for_session(assigned_session)["profile_id"]
        == capture_profile["id"]
    )

    returned_to_default = client.patch(
        f"/api/sessions/{session['id']}/harness-profile",
        json={"harness_profile_id": ""},
    )
    assert returned_to_default.status_code == 200, returned_to_default.text
    default_session = session_manager.get(session["id"])
    assert default_session["harness_profile_id"] == ""
    assert (
        harness_profile_resolver.resolve_for_session(default_session)["profile_id"]
        == "default"
    )
