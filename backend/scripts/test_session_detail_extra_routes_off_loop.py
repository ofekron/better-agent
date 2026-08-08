from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-detail-extra-routes-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
import communication_log  # noqa: E402
import session_detail_api  # noqa: E402
from stores import provenance_store  # noqa: E402
from _test_request import http_request  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _response_json(value: object) -> dict:
    assert isinstance(value, object) and hasattr(value, "body"), (
        f"expected an off-loop Response, got {type(value).__name__}"
    )
    assert value.media_type == "application/json", value.media_type
    body = value.body
    assert isinstance(body, bytes), type(body)
    return json.loads(body)


def _seed_session_with_messages(n: int) -> str:
    sess = session_manager.create(
        name="detail-extra-routes", model="gpt-test", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    for i in range(n):
        session_manager.append_user_msg(sid, {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": f"prompt-{i}",
        })
        session_manager.append_assistant_msg(sid, {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": f"reply-{i}",
            "events": [],
        })
    return sid


def test_get_older_messages_route_matches_manager_result() -> None:
    sid = _seed_session_with_messages(6)
    sess = session_manager.get(sid)
    seqs = sorted(m.get("seq") for m in sess["messages"] if m.get("seq") is not None)
    before_seq = seqs[-1] + 1

    expected = session_manager.get_messages_before(sid, before_seq, 3, exchange_count=None)
    assert expected is not None, "manager returned no result for seeded session"

    response = asyncio.run(session_detail_api.get_older_messages(
        http_request(f"/sessions/{sid}/messages"), sid,
        before_seq=before_seq, limit=3, exchange_count=None,
    ))
    actual = _response_json(response)

    assert actual["total_messages"] == expected["total_messages"]
    assert actual["has_older"] == expected["has_older"]
    assert actual["oldest_loaded_seq"] == expected["oldest_loaded_seq"]
    assert len(actual["messages"]) == len(expected["messages"])
    assert [m["id"] for m in actual["messages"]] == [m["id"] for m in expected["messages"]]


def test_get_older_messages_route_404_for_unknown_session() -> None:
    from fastapi import HTTPException

    try:
        asyncio.run(session_detail_api.get_older_messages(
            http_request("/sessions/does-not-exist/messages"), "does-not-exist",
            before_seq=1, limit=10, exchange_count=None,
        ))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected HTTPException(404) for unknown session")


def test_get_communications_route_matches_direct_call() -> None:
    expected = communication_log.list_communications(session_id="", limit=50)

    response = asyncio.run(session_detail_api.get_communications(
        http_request("/communications"), session_id=None, limit=50,
    ))
    actual = _response_json(response)

    assert actual == expected, "route response diverged from direct list_communications call"


def test_get_session_changes_route_matches_direct_build() -> None:
    sid = _seed_session_with_messages(2)
    sess = session_manager.get(sid)
    changes = provenance_store.read_file_changes(sid)
    expected_turns = provenance_store.group_changes_by_turn(
        sess.get("messages") or [], changes,
    )

    response = asyncio.run(session_detail_api.get_session_changes(
        http_request(f"/sessions/{sid}/changes"), sid,
    ))
    actual = _response_json(response)

    assert actual["session_id"] == sid
    assert actual["turns"] == expected_turns


def test_routes_use_off_loop_serializer_not_raw_dict() -> None:
    source = (session_detail_api.__file__ and open(session_detail_api.__file__, encoding="utf-8").read())
    tree = ast.parse(source)

    def find(name: str) -> ast.AsyncFunctionDef:
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found")

    for name in ("get_older_messages", "get_communications", "get_session_changes"):
        fn = find(name)
        calls = [
            ast.dump(node)
            for node in ast.walk(fn)
            if isinstance(node, ast.Attribute) and node.attr == "json_response_off_loop"
        ]
        assert calls, f"{name} does not route through session_list_cache.json_response_off_loop"
        arg_names = {arg.arg for arg in fn.args.args}
        assert "request" in arg_names, f"{name} lost its `request` param needed for accept-encoding"


def main() -> int:
    tests = [
        ("get_older_messages route matches manager result", test_get_older_messages_route_matches_manager_result),
        ("get_older_messages route 404s for unknown session", test_get_older_messages_route_404_for_unknown_session),
        ("get_communications route matches direct call", test_get_communications_route_matches_direct_call),
        ("get_session_changes route matches direct build", test_get_session_changes_route_matches_direct_build),
        ("routes use off-loop serializer, not raw dict", test_routes_use_off_loop_serializer_not_raw_dict),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"  exception: {exc!r}")
            print(f"{FAIL} {name}")
            failures += 1
        else:
            print(f"{PASS} {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
