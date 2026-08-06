from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-push-notifications-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import device_token_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_bus_subscribers import bind_push_notifications  # noqa: E402
import push_sender  # noqa: E402
import user_input_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_register_unregister_round_trip() -> None:
    record = device_token_store.register_token("dev-1", "tok-abc", "android", "sid-1")
    assert record["device_id"] == "dev-1"
    assert record["platform"] == "android"
    assert record["session_ids"] == ["sid-1"]
    assert record["notification_preferences"] == {
        "pending_approvals": True,
        "pending_questions": True,
        "completed_turns": True,
    }

    tokens = device_token_store.get_tokens_for_session("sid-1")
    assert len(tokens) == 1
    assert tokens[0]["token"] == "tok-abc"

    # Re-registering the same device for a second session accumulates interest.
    device_token_store.register_token("dev-1", "tok-abc", "android", "sid-2")
    tokens_sid2 = device_token_store.get_tokens_for_session("sid-2")
    assert len(tokens_sid2) == 1

    deleted = device_token_store.unregister_token("dev-1")
    assert deleted
    assert not device_token_store.get_tokens_for_session("sid-1")
    assert not device_token_store.unregister_token("dev-1")


def test_preferences_are_durable_validated_and_filter_devices() -> None:
    initial = device_token_store.update_notification_preferences(
        "dev-prefs",
        {"completed_turns": False},
    )
    assert not initial["completed_turns"]
    device_token_store.register_token(
        "dev-prefs",
        "tok-prefs",
        "ios",
        "sid-prefs",
    )
    persisted = device_token_store.get_notification_preferences("dev-prefs")
    assert not persisted["completed_turns"]
    assert not device_token_store.get_tokens_for_session_category(
        "sid-prefs",
        "completed_turns",
    )
    assert len(device_token_store.get_tokens_for_session_category(
        "sid-prefs",
        "pending_questions",
    )) == 1
    try:
        device_token_store.update_notification_preferences(
            "dev-prefs",
            {"completed_turns": "yes"},
        )
        raise AssertionError("invalid preference value should raise NotificationPreferencesError")
    except device_token_store.NotificationPreferencesError:
        pass
    try:
        device_token_store.update_notification_preferences(
            "dev-prefs",
            {"unknown": True},
        )
        raise AssertionError("unknown preference key should raise NotificationPreferencesError")
    except device_token_store.NotificationPreferencesError:
        pass
    device_token_store.unregister_token("dev-prefs")
    assert not device_token_store.get_notification_preferences(
        "dev-prefs"
    )["completed_turns"]


def test_token_re_registration_has_one_device_owner() -> None:
    device_token_store.register_token("dev-old", "tok-shared", "ios", "sid-shared")
    device_token_store.register_token(
        "dev-current",
        "tok-shared",
        "ios",
        "sid-shared",
    )
    devices = device_token_store.get_tokens_for_session("sid-shared")
    assert [device["device_id"] for device in devices] == ["dev-current"]
    device_token_store.unregister_token("dev-current")


def test_send_with_no_service_account_is_safe_noop() -> None:
    os.environ.pop("BETTER_AGENT_FCM_SERVICE_ACCOUNT", None)
    push_sender._INIT_ATTEMPTED = False
    push_sender._APP = None
    device_token_store.register_token("dev-2", "tok-xyz", "ios", "sid-noop")
    push_sender.send_pending_input_push("sid-noop", "approval", "req-1")
    device_token_store.unregister_token("dev-2")


def test_new_pending_request_triggers_push_per_device() -> None:
    calls: list[tuple[str, str, str]] = []
    original = push_sender.send_pending_input_push

    def fake_send(session_id: str, request_kind: str, request_id: str) -> None:
        calls.append((session_id, request_kind, request_id))

    push_sender.send_pending_input_push = fake_send
    try:
        req = user_input_store.create_request(
            app_session_id="sid-push",
            questions=[{"id": "q1", "header": "H", "question": "Q", "options": []}],
            timeout_seconds=60,
        )
        assert len(calls) == 1
        assert calls[0] == ("sid-push", "input", req["request_id"])

        # create_or_get_pending_request against an identical pending request
        # must NOT fire a second push (it's a dedup/update, not a new request).
        again, created = user_input_store.create_or_get_pending_request(
            app_session_id="sid-push",
            questions=[{"id": "q1", "header": "H", "question": "Q", "options": []}],
            timeout_seconds=60,
        )
        assert not created
        assert len(calls) == 1

        second, created2 = user_input_store.create_or_get_pending_request(
            app_session_id="sid-push",
            kind="approval",
            prompt="Proceed?",
            questions=[],
            timeout_seconds=60,
        )
        assert created2
        assert len(calls) == 2
        assert calls[1] == ("sid-push", "approval", second["request_id"])
    finally:
        push_sender.send_pending_input_push = original


def test_successful_turn_emits_configurable_response_push() -> None:
    import asyncio
    import threading

    import session_manager

    calls: list[tuple[str, str | None]] = []
    send_started = threading.Event()
    release_send = threading.Event()
    original = push_sender.send_turn_completed_push

    def fake_send(
        session_id: str, *, message_id: str | None = None,
    ) -> None:
        calls.append((session_id, message_id))
        send_started.set()
        release_send.wait()

    push_sender.send_turn_completed_push = fake_send
    bind_push_notifications()
    # The subscriber resolves the deep-link target via session_manager; stub
    # it so we can assert the latest assistant message id is forwarded.
    original_lookup = session_manager.manager.latest_assistant_msg_id
    session_manager.manager.latest_assistant_msg_id = (
        lambda sid: "msg-latest" if sid == "sid-complete" else None
    )
    try:
        async def exercise() -> None:
            publish_task = asyncio.create_task(bus.publish(BusEvent(
                type="lifecycle.turn_complete",
                root_id="sid-complete",
                sid="sid-complete",
                payload={"reason": "success"},
                persist=False,
            )))
            started = await asyncio.to_thread(send_started.wait, 1.0)
            await asyncio.sleep(0)
            completed_before_delivery = publish_task.done()
            release_send.set()
            await publish_task
            await bus.publish(BusEvent(
                type="lifecycle.turn_complete",
                root_id="sid-error",
                sid="sid-error",
                payload={"reason": "error"},
                persist=False,
            ))
            assert started, "push send did not start"
            assert completed_before_delivery, "publish completed before delivery"
            assert calls == [("sid-complete", "msg-latest")], f"unexpected calls: {calls}"

        asyncio.run(exercise())
    finally:
        release_send.set()
        bus.unsubscribe("push_notification_turn_complete")
        session_manager.manager.latest_assistant_msg_id = original_lookup
        push_sender.send_turn_completed_push = original


TESTS = [
    test_register_unregister_round_trip,
    test_preferences_are_durable_validated_and_filter_devices,
    test_token_re_registration_has_one_device_owner,
    test_send_with_no_service_account_is_safe_noop,
    test_new_pending_request_triggers_push_per_device,
    test_successful_turn_emits_configurable_response_push,
]


def main() -> int:
    failures = 0
    try:
        for test in TESTS:
            try:
                test()
            except Exception as exc:
                failures += 1
                print(f"{FAIL} {test.__name__}: {exc}")
            else:
                print(f"{PASS} {test.__name__}")
        return 1 if failures else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
