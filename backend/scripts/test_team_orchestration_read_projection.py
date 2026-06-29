from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-team-orch-read-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from session_manager import manager as session_manager  # noqa: E402
from stores import worker_store  # noqa: E402
import team_orchestration_read  # noqa: E402


def test_worker_projection_uses_summary_fields_before_full_session_read() -> None:
    worker = session_manager.create(
        name="summary worker",
        cwd="/repo/worker",
        orchestration_mode="native",
        source="cli",
    )
    worker_store.upsert_worker(
        "/repo/worker",
        worker["id"],
        "native",
        "agent-summary-worker",
    )

    original = team_orchestration_read.session_manager.get_fields_many

    def fail_full_read(*_args, **_kwargs):
        raise AssertionError("worker projection loaded full session fields")

    team_orchestration_read.session_manager.get_fields_many = fail_full_read
    try:
        projected = team_orchestration_read.list_workers_for_cwd("/repo/worker")
    finally:
        team_orchestration_read.session_manager.get_fields_many = original

    assert projected["workers"][0]["agent_session_id"] == worker["id"]
    assert projected["workers"][0]["display_name"] == "summary worker"


def main() -> int:
    test_worker_projection_uses_summary_fields_before_full_session_read()
    print("PASS team orchestration read projection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
