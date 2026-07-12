from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-user-input-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import session_store  # noqa: E402
import user_input_store  # noqa: E402
from user_input_contract import USER_INPUT_MAX_QUESTIONS  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session() -> str:
    return session_store.create_session(
        name="user-input",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )["id"]


def test_store_persists_pending_request() -> bool:
    sid = _new_session()
    req = user_input_store.create_request(
        app_session_id=sid,
        questions=[{"id": "choice", "header": "Pick", "question": "Which?", "options": []}],
        timeout_seconds=60,
    )
    pending = user_input_store.pending_for_session(sid)
    counts = user_input_store.pending_counts_by_session()
    return (
        len(pending) == 1
        and pending[0]["request_id"] == req["request_id"]
        and user_input_store.pending_count_for_session(sid) == 1
        and counts.get(sid) == 1
    )


def test_internal_request_waits_until_browser_resolves(client: TestClient) -> bool:
    sid = _new_session()
    token = main.coordinator.internal_token
    result_holder: dict = {}

    def post_request() -> None:
        result_holder["response"] = client.post(
            "/api/internal/user-input/request",
            headers={"X-Internal-Token": token},
            json={
                "app_session_id": sid,
                "questions": [{
                    "id": "decision",
                    "header": "Decision",
                    "question": "Proceed?",
                    "options": [{"label": "Yes", "description": "Continue"}],
                }],
                "timeout_seconds": 5,
            },
        )

    t = threading.Thread(target=post_request)
    t.start()
    request_id = ""
    deadline = time.time() + 3
    while time.time() < deadline:
        pending = user_input_store.pending_for_session(sid)
        if pending:
            request_id = pending[0]["request_id"]
            break
        time.sleep(0.02)
    if not request_id:
        return False
    resolve = client.post(
        f"/api/user-input/{request_id}/resolve",
        json={"app_session_id": sid, "answers": {"decision": "Yes"}},
    )
    t.join(timeout=3)
    if t.is_alive() or resolve.status_code != 200:
        return False
    response = result_holder.get("response")
    if response is None or response.status_code != 200:
        return False
    data = response.json()
    return (
        data.get("success") is True
        and data.get("answers") == {"decision": "Yes"}
        and user_input_store.pending_count_for_session(sid) == 0
    )


def test_duplicate_internal_request_reuses_pending_dialog(client: TestClient) -> bool:
    sid = _new_session()
    other_sid = _new_session()
    token = main.coordinator.internal_token
    questions = [{
        "id": "decision",
        "header": "Decision",
        "question": "Proceed?",
        "options": [{"label": "Yes", "description": "Continue"}],
    }]
    requested_events: list[dict] = []
    state_events: list[dict] = []
    original_broadcast = main._broadcast_user_input
    original_state = main._broadcast_user_input_state

    async def fake_broadcast(event_type: str, payload: dict) -> None:
        if event_type == "user_input_requested":
            requested_events.append(payload)
        await original_broadcast(event_type, payload)

    async def fake_state(app_session_id: str) -> None:
        state_events.append({"app_session_id": app_session_id})
        await original_state(app_session_id)

    main._broadcast_user_input = fake_broadcast
    main._broadcast_user_input_state = fake_state
    responses: list = []

    def post_request(timeout_seconds: float) -> None:
        responses.append(client.post(
            "/api/internal/user-input/request",
            headers={"X-Internal-Token": token},
            json={
                "app_session_id": sid,
                "questions": questions,
                "timeout_seconds": timeout_seconds,
            },
        ))

    threads = [
        threading.Thread(target=post_request, args=(5,)),
        threading.Thread(target=post_request, args=(1,)),
    ]
    try:
        for t in threads:
            t.start()
        deadline = time.time() + 3
        pending_snapshot: list[dict] = []
        while time.time() < deadline:
            pending_snapshot = user_input_store.pending_for_session(sid)
            if pending_snapshot:
                time.sleep(0.1)
                break
            time.sleep(0.02)
        pending_count = user_input_store.pending_count_for_session(sid)
        if not pending_snapshot:
            return False
        pending_expires_at = pending_snapshot[0].get("expires_at")
        same_req, same_created = user_input_store.create_or_get_pending_request(
            app_session_id=sid,
            questions=questions,
            timeout_seconds=100,
        )
        if (
            same_created is not False
            or same_req["request_id"] != pending_snapshot[0]["request_id"]
            or same_req.get("expires_at") != pending_expires_at
        ):
            return False
        resolve = client.post(
            f"/api/user-input/{pending_snapshot[0]['request_id']}/resolve",
            json={"app_session_id": sid, "answers": {"decision": "Yes"}},
        )
        for t in threads:
            t.join(timeout=3)
        if any(t.is_alive() for t in threads) or resolve.status_code != 200:
            return False
    finally:
        main._broadcast_user_input = original_broadcast
        main._broadcast_user_input_state = original_state
    if len(responses) != 2 or any(r.status_code != 200 for r in responses):
        return False
    bodies = [r.json() for r in responses]
    if (
        pending_count != 1
        or len(requested_events) != 1
        or len({body.get("request_id") for body in bodies}) != 1
        or any(body.get("success") is not True for body in bodies)
        or any(body.get("answers") != {"decision": "Yes"} for body in bodies)
        or user_input_store.pending_count_for_session(sid) != 0
    ):
        return False
    next_req, next_created = user_input_store.create_or_get_pending_request(
        app_session_id=sid,
        questions=questions,
        timeout_seconds=60,
    )
    other_req, other_created = user_input_store.create_or_get_pending_request(
        app_session_id=other_sid,
        questions=questions,
        timeout_seconds=60,
    )
    return (
        next_created is True
        and other_created is True
        and next_req["request_id"] != bodies[0]["request_id"]
        and other_req["request_id"] != next_req["request_id"]
    )


def test_validation_rejects_bad_question_shape(client: TestClient) -> bool:
    sid = _new_session()
    res = client.post(
        "/api/internal/user-input/request",
        headers={"X-Internal-Token": main.coordinator.internal_token},
        json={"app_session_id": sid, "questions": []},
    )
    data = res.json()
    return res.status_code == 200 and data.get("success") is False


def test_validation_accepts_question_batches() -> bool:
    questions = [
        {"id": f"q{index}", "header": f"Question {index}", "question": "Answer?", "options": []}
        for index in range(USER_INPUT_MAX_QUESTIONS)
    ]
    accepted = main._validate_user_input_questions(questions)
    if len(accepted) != USER_INPUT_MAX_QUESTIONS:
        return False
    try:
        main._validate_user_input_questions([
            *questions,
            {"id": "overflow", "header": "Overflow", "question": "Too many?", "options": []},
        ])
    except Exception as exc:
        return "questions must contain" in str(exc)
    return False


def test_sidebar_decoration_exposes_pending_count() -> bool:
    sid = _new_session()
    user_input_store.create_request(
        app_session_id=sid,
        questions=[{"id": "q", "header": "H", "question": "Q", "options": []}],
        timeout_seconds=60,
    )
    rows = main._decorate_local_sidebar_sessions([{
        "id": sid,
        "name": "user-input",
        "cwd": "/tmp",
        "node_id": "primary",
    }])
    return len(rows) == 1 and rows[0].get("pending_user_input_count") == 1


def test_request_payload_is_session_scoped() -> bool:
    sid = _new_session()
    req = user_input_store.create_request(
        app_session_id=sid,
        questions=[{"id": "q", "header": "H", "question": "Secret?", "options": []}],
        timeout_seconds=60,
    )
    direct: list[tuple[str, dict]] = []
    global_events: list[tuple[str, dict]] = []
    original_dispatch = main.coordinator.dispatch_raw
    original_global = main.coordinator.broadcast_global

    async def fake_dispatch(target_sid: str, payload: dict) -> None:
        direct.append((target_sid, payload))

    async def fake_global(event_type: str, payload: dict) -> None:
        global_events.append((event_type, payload))

    main.coordinator.dispatch_raw = fake_dispatch
    main.coordinator.broadcast_global = fake_global
    try:
        asyncio.run(main._broadcast_user_input("user_input_requested", req))
        asyncio.run(main._broadcast_user_input_state(sid))
    finally:
        main.coordinator.dispatch_raw = original_dispatch
        main.coordinator.broadcast_global = original_global
    return (
        direct == [(sid, {"type": "user_input_requested", "data": req})]
        and len(global_events) == 1
        and global_events[0][0] == "session_user_input_changed"
        and global_events[0][1].get("pending_user_input_count") == 1
        and "questions" not in global_events[0][1]
    )


def test_pending_counts_are_cached_after_warmup() -> bool:
    sid = _new_session()
    user_input_store.create_request(
        app_session_id=sid,
        questions=[{"id": "q", "header": "H", "question": "Q", "options": []}],
        timeout_seconds=60,
    )
    if user_input_store.pending_count_for_session(sid) != 1:
        return False
    original = user_input_store._read_locked
    original_path = user_input_store._path

    def fail_read():
        raise AssertionError("pending count hot path read store")

    def fail_path():
        raise AssertionError("pending count hot path resolved store path")

    user_input_store._read_locked = fail_read
    user_input_store._path = fail_path
    try:
        return (
            user_input_store.pending_count_for_session(sid) == 1
            and user_input_store.pending_counts_by_session().get(sid) == 1
        )
    finally:
        user_input_store._read_locked = original
        user_input_store._path = original_path


def run() -> int:
    client = TestClient(main.app)
    authenticate_client(client)
    tests = [
        ("store persists pending request", lambda: test_store_persists_pending_request()),
        ("internal request waits until browser resolves", lambda: test_internal_request_waits_until_browser_resolves(client)),
        ("duplicate internal request reuses pending dialog", lambda: test_duplicate_internal_request_reuses_pending_dialog(client)),
        ("validation rejects bad question shape", lambda: test_validation_rejects_bad_question_shape(client)),
        ("validation accepts question batches", lambda: test_validation_accepts_question_batches()),
        ("sidebar decoration exposes pending count", lambda: test_sidebar_decoration_exposes_pending_count()),
        ("request payload is session scoped", lambda: test_request_payload_is_session_scoped()),
        ("pending counts are cached after warmup", lambda: test_pending_counts_are_cached_after_warmup()),
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            ok = bool(fn())
        except Exception as exc:
            ok = False
            print(f"  {name} raised: {exc}")
        print(f"{PASS if ok else FAIL} {name}")
        if not ok:
            failures.append(name)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
