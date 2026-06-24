"""Ownership-gated session-message mutation (the last SDK primitive).

Locks: an extension can only append/update/set-streaming on sessions it
created via /api/internal/create-session (or create-sub-session). The caller's
identity is derived from its per-extension token (not a header). A second
extension, a session created without an extension identity, a wrong token, or a
missing message_id are all rejected. Mutations persist into the session record.
SDK client methods hit the right paths.

Run standalone:  python scripts/test_extension_sdk_session_mutation.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.request

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sdkmut-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
for _p in (_BACKEND, os.path.join(_REPO, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from starlette.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import extension_store  # noqa: E402
import extension_session_ownership  # noqa: E402
import extension_token_registry  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from better_agent_sdk import Client  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


CLIENT = TestClient(main.app, client=("127.0.0.1", 50003))
TOKEN = main.coordinator.internal_token
OWNER_EXT = "test.mut-owner"
OTHER_EXT = "test.mut-other"


def _seed(extension_id: str) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {"id": extension_id, "permissions": {}},
        "enabled": True,
        "source": {"type": "git", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)


_SENTINEL = object()


def _hdr(extension_id, token=_SENTINEL):
    # Identity is token-derived: acting as an extension = sending its token.
    if token is _SENTINEL:
        token = extension_token_registry.mint(extension_id)
    return {"X-Internal-Token": token}


def _post(path, body, extension_id, token=_SENTINEL):
    return CLIENT.post(path, json=body, headers=_hdr(extension_id, token))


def main_test() -> int:
    _seed(OWNER_EXT)
    _seed(OTHER_EXT)

    print("M1 create-session claims ownership for the calling extension")
    r = CLIENT.post(
        "/api/internal/create-session",
        json={"name": "owned-sess", "cwd": _TMP_HOME},
        headers=_hdr(OWNER_EXT),
    )
    check(r.status_code == 200, f"create-session ok (got {r.status_code} {r.text[:200]})")
    owned_sid = r.json()["session_id"]
    check(extension_session_ownership.owner(owned_sid) == OWNER_EXT, "ownership recorded")

    # A session created WITHOUT an extension id is NOT owned by anyone.
    bare_sid = session_manager.create(name="bare", cwd=_TMP_HOME)["id"]
    check(extension_session_ownership.owner(bare_sid) is None, "non-extension session unowned")

    print("M2 append: owner allowed, others rejected")
    r = _post(
        "/api/internal/session-messages/append",
        {"session_id": owned_sid, "role": "user", "content": "hello"},
        OWNER_EXT,
    )
    check(r.status_code == 200 and r.json()["message"]["role"] == "user", "owner appends user msg")
    msg_id = r.json()["message"]["id"]
    msgs = [m for m in session_manager.get(owned_sid)["messages"] if m.get("id") == msg_id]
    check(len(msgs) == 1 and msgs[0]["content"] == "hello", "append persisted into session")

    r = _post(
        "/api/internal/session-messages/append",
        {"session_id": owned_sid, "role": "user", "content": "x"},
        OTHER_EXT,
    )
    check(r.status_code == 403, f"non-owner extension -> 403 (got {r.status_code})")
    r = _post(
        "/api/internal/session-messages/append",
        {"session_id": bare_sid, "role": "user", "content": "x"},
        OWNER_EXT,
    )
    check(r.status_code == 403, f"unowned session -> 403 (got {r.status_code})")
    r = _post(
        "/api/internal/session-messages/append",
        {"session_id": owned_sid, "role": "weird", "content": "x"},
        OWNER_EXT,
    )
    check(r.status_code == 400, f"bad role -> 400 (got {r.status_code})")
    r = _post(
        "/api/internal/session-messages/append",
        {"session_id": owned_sid, "role": "user", "content": "x"},
        OWNER_EXT,
        token="wrong",
    )
    check(r.status_code == 403, f"wrong token -> 403 (got {r.status_code})")

    print("M3 update-content + set-streaming gated by ownership")
    _post(
        "/api/internal/session-messages/append",
        {"session_id": owned_sid, "role": "assistant", "content": "draft", "message_id": "a1"},
        OWNER_EXT,
    )
    r = _post(
        "/api/internal/session-messages/update-content",
        {"session_id": owned_sid, "message_id": "a1", "content": "final"},
        OWNER_EXT,
    )
    check(r.status_code == 200, "owner updates content")
    a1 = next(m for m in session_manager.get(owned_sid)["messages"] if m["id"] == "a1")
    check(a1["content"] == "final", "update-content persisted")
    r = _post(
        "/api/internal/session-messages/update-content",
        {"session_id": owned_sid, "content": "x"},
        OWNER_EXT,
    )
    check(r.status_code == 400, f"missing message_id -> 400 (got {r.status_code})")
    r = _post(
        "/api/internal/session-messages/update-content",
        {"session_id": owned_sid, "message_id": "a1", "content": "x"},
        OTHER_EXT,
    )
    check(r.status_code == 403, f"non-owner update -> 403 (got {r.status_code})")

    r = _post(
        "/api/internal/session-messages/set-streaming",
        {"session_id": owned_sid, "message_id": "a1", "streaming": False},
        OWNER_EXT,
    )
    check(r.status_code == 200, "owner sets streaming")
    a1 = next(m for m in session_manager.get(owned_sid)["messages"] if m["id"] == "a1")
    check(a1.get("isStreaming") is False, "set-streaming persisted")

    print("M4 ownership store helpers")
    extension_session_ownership.claim("synthetic-sid", OWNER_EXT)
    check(extension_session_ownership.is_owner("synthetic-sid", OWNER_EXT), "claim -> is_owner")
    check(not extension_session_ownership.is_owner("synthetic-sid", OTHER_EXT), "is_owner false for non-owner")
    extension_session_ownership.disown("synthetic-sid")
    check(not extension_session_ownership.is_owner("synthetic-sid", OWNER_EXT), "disown removes ownership")

    print("M5 SDK client methods hit the right paths/payloads")
    captured: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode("utf-8") if req.data else ""
        return _FakeResp()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        c = Client(internal_token="tok", extension_id=OWNER_EXT, app_session_id=owned_sid, backend_url="http://core")
        c.append_session_message("assistant", "hi")
        body = json.loads(captured["data"])
        check(captured["url"].endswith("/api/internal/session-messages/append")
              and body == {"session_id": owned_sid, "role": "assistant", "content": "hi",
                           "message_id": "", "timestamp": "", "is_streaming": False},
              "append_session_event -> right path + payload")
        c.update_session_message_content("m1", "new")
        body = json.loads(captured["data"])
        check(captured["url"].endswith("/api/internal/session-messages/update-content")
              and body["message_id"] == "m1" and body["content"] == "new", "update method -> right path + payload")
    finally:
        urllib.request.urlopen = original_urlopen

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: extension sdk ownership-gated session mutation")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
