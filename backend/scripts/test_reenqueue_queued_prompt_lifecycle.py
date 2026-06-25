from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-reenqueue-lifecycle-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_queue_projection  # noqa: E402


class _SessionManager:
    def __init__(self) -> None:
        self.session = {
            "id": "sid",
            "model": "m",
            "cwd": "/tmp/reenqueue",
            "messages": [],
            "queued_prompts": [{
                "id": "q1",
                "content": "persisted prompt",
                "client_id": "client-1",
                "disallowed_tools": ["Bash", "Edit"],
                "orchestration_mode": "native",
            }],
        }
        self.updated: list[tuple[str, str | None, dict]] = []

    def get(self, sid: str) -> dict | None:
        return self.session if sid == "sid" else None

    def get_lite(self, sid: str) -> dict | None:
        return self.get(sid)

    def update_queued_prompt(
        self,
        sid: str,
        queued_id: str | None,
        updates: dict,
    ) -> None:
        self.updated.append((sid, queued_id, updates))
        for prompt in self.session["queued_prompts"]:
            if prompt.get("id") == queued_id:
                prompt.update(updates)

    def remove_queued_prompt(self, *_args) -> None:
        raise AssertionError("queued prompt should not be removed")

    def rebuild_queued_prompt_counts(self) -> None:
        pass


class _Coordinator:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def is_prompt_item_in_flight(self, sid: str, item_id: str) -> bool:
        # Startup re-enqueue runs against a cold coordinator; nothing is in-flight.
        return False

    def submit_prompt(self, sid: str, params: dict) -> str:
        self.submitted.append((sid, params))
        return "queued-runtime-id"

    async def submit_prompt_async(self, sid: str, params: dict) -> str:
        return self.submit_prompt(sid, params)


def main_test() -> int:
    real_session_manager = main.session_manager
    real_coordinator = main.coordinator
    real_projection_list = session_queue_projection.list_queued_records
    real_projection_ensure = session_queue_projection.ensure_current_or_rebuild
    fake_session_manager = _SessionManager()
    fake_coordinator = _Coordinator()
    main.session_manager = fake_session_manager
    main.coordinator = fake_coordinator
    session_queue_projection.list_queued_records = lambda: [fake_session_manager.session]
    session_queue_projection.ensure_current_or_rebuild = lambda: False
    try:
        asyncio.run(main._re_enqueue_queued_prompts())
        assert len(fake_session_manager.updated) == 1
        _, queued_id, updates = fake_session_manager.updated[0]
        assert queued_id == "q1"
        lifecycle_msg_id = updates.get("lifecycle_msg_id")
        assert isinstance(lifecycle_msg_id, str) and lifecycle_msg_id
        assert fake_coordinator.submitted[0][1]["lifecycle_msg_id"] == lifecycle_msg_id
        assert fake_coordinator.submitted[0][1]["disallowed_tools"] == ["Bash", "Edit"]
        assert fake_session_manager.session["queued_prompts"][0][
            "lifecycle_msg_id"
        ] == lifecycle_msg_id
        print("PASS re-enqueue persists missing queued prompt lifecycle id")
        return 0
    finally:
        session_queue_projection.ensure_current_or_rebuild = real_projection_ensure
        session_queue_projection.list_queued_records = real_projection_list
        main.coordinator = real_coordinator
        main.session_manager = real_session_manager
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_test())
