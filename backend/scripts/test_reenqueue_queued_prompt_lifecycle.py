from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-reenqueue-lifecycle-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import recovery  # noqa: E402
import lifecycle_command_store  # noqa: E402
import session_queue_projection  # noqa: E402


class _SessionManager:
    def __init__(self) -> None:
        self._root_repository = self
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
                "delivery_attempt": 1,
            }],
        }
        self.updated: list[tuple[str, str | None, dict]] = []

    def storage_identity(self) -> str:
        return "test-storage"

    def get(self, sid: str) -> dict | None:
        return self.session if sid == "sid" else None

    def get_lite(self, sid: str) -> dict | None:
        return self.get(sid)

    def is_live_session(self, sid: str) -> bool:
        # The lifecycle test's session is live; the re-enqueue guard must pass.
        return sid == "sid"

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

    def reserve_queued_prompt_delivery_attempt(
        self, sid: str, queued_id: str,
    ) -> int:
        for prompt in self.session["queued_prompts"]:
            if prompt.get("id") == queued_id:
                prompt["delivery_attempt"] = (
                    int(prompt.get("delivery_attempt") or 0) + 1
                )
                return prompt["delivery_attempt"]
        raise AssertionError("queued prompt missing")

    def rebuild_queued_prompt_counts(self) -> None:
        pass


class _Coordinator:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []
        self.start_processor_values: list[bool] = []
        self.lifecycle_commands = SimpleNamespace(
            snapshot=lambda _sid: SimpleNamespace(identity=None),
        )

    def is_prompt_item_in_flight(self, sid: str, item_id: str) -> bool:
        # Startup re-enqueue runs against a cold coordinator; nothing is in-flight.
        return False

    def submit_prompt(self, sid: str, params: dict) -> str:
        self.submitted.append((sid, params))
        return "queued-runtime-id"

    async def submit_prompt_async(
        self,
        sid: str,
        params: dict,
        *,
        start_processor: bool = True,
    ) -> str:
        self.start_processor_values.append(start_processor)
        return self.submit_prompt(sid, params)


def main_test() -> int:
    real_session_manager = recovery.session_manager
    real_coordinator = recovery._coordinator_ref
    real_projection_list = session_queue_projection.list_queued_records
    real_projection_ensure = session_queue_projection.ensure_current_or_rebuild
    real_transition_for = lifecycle_command_store.transition_for
    fake_session_manager = _SessionManager()
    fake_coordinator = _Coordinator()
    recovery.session_manager = fake_session_manager
    recovery._coordinator_ref = fake_coordinator
    session_queue_projection.list_queued_records = (
        lambda **_kwargs: [fake_session_manager.session]
    )
    session_queue_projection.ensure_current_or_rebuild = lambda **_kwargs: False
    no_transition = lambda *_args: None
    lifecycle_command_store.transition_for = no_transition
    try:
        rehydrated_session_ids = asyncio.run(recovery._re_enqueue_queued_prompts())
        assert rehydrated_session_ids == {"sid"}
        assert fake_coordinator.start_processor_values == [False]
        assert len(fake_session_manager.updated) == 1
        _, queued_id, updates = fake_session_manager.updated[0]
        assert queued_id == "q1"
        lifecycle_msg_id = updates.get("lifecycle_msg_id")
        assert isinstance(lifecycle_msg_id, str) and lifecycle_msg_id
        assert fake_coordinator.submitted[0][1]["lifecycle_msg_id"] == lifecycle_msg_id
        assert fake_coordinator.submitted[0][1]["disallowed_tools"] == ["Bash", "Edit"]
        assert fake_coordinator.submitted[0][1]["_delivery_attempt"] == 1
        assert fake_session_manager.session["queued_prompts"][0][
            "lifecycle_msg_id"
        ] == lifecycle_msg_id

        lifecycle_command_store.transition_for = lambda *_args: {"status": "done"}
        try:
            retired_attempt = asyncio.run(
                recovery._reconcile_queued_delivery_attempt(
                    "sid", "q1", lifecycle_msg_id, 1,
                )
            )
        finally:
            lifecycle_command_store.transition_for = no_transition
        assert retired_attempt == 2
        assert fake_session_manager.session["queued_prompts"][0][
            "delivery_attempt"
        ] == 2

        active_identity = SimpleNamespace(
            lifecycle_message_id=lifecycle_msg_id,
        )
        finished: list[str] = []

        async def finish_turn(**_kwargs) -> None:
            finished.append("finished")

        fake_coordinator.lifecycle_commands = SimpleNamespace(
            snapshot=lambda _sid: SimpleNamespace(
                identity=active_identity,
                execution=None,
            ),
            finish_turn=finish_turn,
        )
        active_attempt = asyncio.run(
            recovery._reconcile_queued_delivery_attempt(
                "sid", "q1", lifecycle_msg_id, 2,
            )
        )
        assert active_attempt == 2
        assert finished == ["finished"]
        print("PASS re-enqueue persists missing queued prompt lifecycle id")
        return 0
    finally:
        session_queue_projection.ensure_current_or_rebuild = real_projection_ensure
        session_queue_projection.list_queued_records = real_projection_list
        lifecycle_command_store.transition_for = real_transition_for
        recovery._coordinator_ref = real_coordinator
        recovery.session_manager = real_session_manager
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_test())
