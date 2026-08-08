"""`TurnManager._publish_typed_prompt_journal` — the backend-authored
TYPED_PROMPT-shaped journal row for the user's own prompt — must carry the
dispatch-time correlator (`user_msg["client_id"]`, itself
`surface_commands.send_prompt`'s unified `cid`, fed by EITHER the v2
intent transport's `intent_id` or the legacy WS transport's `client_id`)
into the published row's `data["intent_id"]`, so `normalize._handle_user`
round-trips it into `TypedPromptPayload.intent_id` and a client-side
optimistic send can reconcile against the confirmed node. Before this fix,
`data` never included the key at all — `TypedPromptPayload.intent_id` was
always None on every produced node, regardless of what the client sent.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_tm_prompt_journal_")

import event_journal  # noqa: E402
from turn_manager import TurnManager  # noqa: E402


class _StubCoordinator:
    pass


def _capture_publish_event() -> tuple[list[dict], object]:
    calls: list[dict] = []
    original = event_journal.publish_event

    async def _fake_publish_event(**kwargs):
        calls.append(kwargs)
        return None

    event_journal.publish_event = _fake_publish_event
    return calls, original


def _restore_publish_event(original) -> None:
    event_journal.publish_event = original


def test_publish_typed_prompt_journal_stamps_intent_id_from_client_id() -> None:
    calls, original = _capture_publish_event()
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_typed_prompt_journal(
            app_session_id="sid-1",
            user_msg={
                "id": "prompt-1",
                "content": "hello",
                "client_id": "client-abc-123",
            },
            assistant_message_id="assistant-1",
        ))
        assert len(calls) == 1, f"expected 1 publish_event call, got {len(calls)}"
        data = calls[0]["data"]
        assert data["intent_id"] == "client-abc-123", (
            f"expected data['intent_id'] to carry user_msg['client_id'], "
            f"got {data.get('intent_id')!r}"
        )
        assert data["uuid"] == "prompt-1"
    finally:
        _restore_publish_event(original)


def test_publish_typed_prompt_journal_ingest_from_v2_intent_client_id() -> None:
    """The v2 intent ingress path funnels `SendPrompt.intent_id` into
    `surface_commands.send_prompt`'s `intent_id` positional arg, which
    becomes `cid` (client_id kwarg unset -> falls back to intent_id — see
    `send_prompt`'s own docstring), threaded through `run_turn(client_id=
    cid)` -> `_init_turn_messages(client_id=cid)` -> `user_msg["client_id"]`
    unchanged. This test locks that the SAME row field is stamped
    regardless of which ingress produced the client_id — single source of
    truth, not two separate code paths."""
    calls, original = _capture_publish_event()
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_typed_prompt_journal(
            app_session_id="sid-2",
            user_msg={
                "id": "prompt-2",
                "content": "hi from v2",
                # Simulates the v2 intent transport's own intent_id, which
                # `surface_commands.send_prompt` unifies into the SAME
                # `client_id` field `_init_turn_messages` stamps on
                # `user_msg` regardless of ingress.
                "client_id": "intent-xyz-789",
            },
            assistant_message_id="assistant-2",
        ))
        assert calls[0]["data"]["intent_id"] == "intent-xyz-789"
    finally:
        _restore_publish_event(original)


def test_publish_typed_prompt_journal_omits_intent_id_when_absent() -> None:
    """Closed-set payload discipline: no client_id/intent_id supplied ->
    no `intent_id` key at all, never a null/empty placeholder."""
    calls, original = _capture_publish_event()
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_typed_prompt_journal(
            app_session_id="sid-3",
            user_msg={"id": "prompt-3", "content": "no correlator"},
            assistant_message_id="assistant-3",
        ))
        assert "intent_id" not in calls[0]["data"]
    finally:
        _restore_publish_event(original)


def test_publish_typed_prompt_journal_omits_intent_id_for_empty_string() -> None:
    calls, original = _capture_publish_event()
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_typed_prompt_journal(
            app_session_id="sid-4",
            user_msg={"id": "prompt-4", "content": "empty cid", "client_id": ""},
            assistant_message_id="assistant-4",
        ))
        assert "intent_id" not in calls[0]["data"]
    finally:
        _restore_publish_event(original)


if __name__ == "__main__":
    test_publish_typed_prompt_journal_stamps_intent_id_from_client_id()
    test_publish_typed_prompt_journal_ingest_from_v2_intent_client_id()
    test_publish_typed_prompt_journal_omits_intent_id_when_absent()
    test_publish_typed_prompt_journal_omits_intent_id_for_empty_string()
    print("OK: TurnManager._publish_typed_prompt_journal intent_id propagation")
