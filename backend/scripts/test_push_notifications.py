from __future__ import annotations

import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-push-notifications-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import device_token_store  # noqa: E402
import push_sender  # noqa: E402
import user_input_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_register_unregister_round_trip() -> bool:
    record = device_token_store.register_token("dev-1", "tok-abc", "android", "sid-1")
    if record["device_id"] != "dev-1" or record["platform"] != "android":
        return False
    if record["session_ids"] != ["sid-1"]:
        return False

    tokens = device_token_store.get_tokens_for_session("sid-1")
    if len(tokens) != 1 or tokens[0]["token"] != "tok-abc":
        return False

    # Re-registering the same device for a second session accumulates interest.
    device_token_store.register_token("dev-1", "tok-abc", "android", "sid-2")
    tokens_sid2 = device_token_store.get_tokens_for_session("sid-2")
    if len(tokens_sid2) != 1:
        return False

    deleted = device_token_store.unregister_token("dev-1")
    if not deleted:
        return False
    if device_token_store.get_tokens_for_session("sid-1"):
        return False
    if device_token_store.unregister_token("dev-1"):
        return False
    return True


def test_send_with_no_service_account_is_safe_noop() -> bool:
    os.environ.pop("BETTER_AGENT_FCM_SERVICE_ACCOUNT", None)
    push_sender._INIT_ATTEMPTED = False
    push_sender._APP = None
    device_token_store.register_token("dev-2", "tok-xyz", "ios", "sid-noop")
    try:
        push_sender.send_pending_input_push("sid-noop", "approval", "req-1")
    except Exception:
        return False
    device_token_store.unregister_token("dev-2")
    return True


def test_new_pending_request_triggers_push_per_device() -> bool:
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
        if len(calls) != 1:
            return False
        if calls[0] != ("sid-push", "input", req["request_id"]):
            return False

        # create_or_get_pending_request against an identical pending request
        # must NOT fire a second push (it's a dedup/update, not a new request).
        again, created = user_input_store.create_or_get_pending_request(
            app_session_id="sid-push",
            questions=[{"id": "q1", "header": "H", "question": "Q", "options": []}],
            timeout_seconds=60,
        )
        if created:
            return False
        if len(calls) != 1:
            return False

        second, created2 = user_input_store.create_or_get_pending_request(
            app_session_id="sid-push",
            kind="approval",
            prompt="Proceed?",
            questions=[],
            timeout_seconds=60,
        )
        if not created2:
            return False
        if len(calls) != 2:
            return False
        if calls[1] != ("sid-push", "approval", second["request_id"]):
            return False
        return True
    finally:
        push_sender.send_pending_input_push = original


TESTS = [
    test_register_unregister_round_trip,
    test_send_with_no_service_account_is_safe_noop,
    test_new_pending_request_triggers_push_per_device,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        ok = test()
        print(f"{PASS if ok else FAIL} {test.__name__}")
        if not ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
