from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-team-store-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import team_store  # noqa: E402
from orchs.manager import bootstrap  # noqa: E402


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_team_store_tracks_members_by_session() -> None:
    team = team_store.create(
        team_id="team-test",
        root_session_id="manager-session",
        definition_ref="extension:ofek.testape:testape-ui-expert",
        profile="web-ui",
    )

    assert team["id"] == "team-test"
    assert team["members"]["manager"]["agent_session_id"] == "manager-session"

    member = team_store.upsert_member(
        "team-test",
        member_id="web-device-worker",
        member_type="worker",
        agent_session_id="worker-session",
        role="testape:web-device-worker",
        description="Web worker",
        cwd="/repo",
        run_mode="direct",
    )

    assert member["agent_session_id"] == "worker-session"
    found = team_store.find_for_session("worker-session")
    assert found is not None
    assert found["id"] == "team-test"
    assert team_store.member_for_session(found, "worker-session")["id"] == "web-device-worker"


def test_team_context_prefers_runtime_team_roster() -> None:
    team_store.create(team_id="team-context", root_session_id="manager-context")
    team_store.upsert_member(
        "team-context",
        member_id="result-auditor",
        member_type="worker",
        agent_session_id="auditor-session",
        role="testape:result-auditor",
        description="Audits evidence",
        cwd="/repo",
        run_mode="fork",
    )

    prompt = bootstrap.format_team_context(
        cwd="/repo",
        self_session_id="auditor-session",
        self_role="worker",
        self_description="fallback",
        workers=[],
        manager_session_id="manager-context",
    )

    assert '<member id="manager" session_id="manager-context" role="manager"' in prompt
    assert '<member id="result-auditor" session_id="auditor-session" role="testape:result-auditor"' in prompt
    assert 'type="worker"' in prompt
    assert "<description>fallback</description>" in prompt


if __name__ == "__main__":
    try:
        test_team_store_tracks_members_by_session()
        test_team_context_prefers_runtime_team_roster()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS team store")
