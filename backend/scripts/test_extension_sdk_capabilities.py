"""Contract tests for the generic SDK capability endpoints added beyond the
core provisioned-sessions primitive:

  GET  /api/internal/provisioned-sessions/specs  — discover invocable specs
  POST /api/internal/broadcast-session           — extension-emitted WS event
  POST /api/internal/session-fields              — declared session-field reads
  POST /api/internal/project-updates/list        — project-structure-owned unseen updates for a project
  POST /api/internal/project-updates/mark-seen   — project-structure-owned mark + broadcast

Locks: token gate on all; broadcast-session requires an active extension and
pins source to it; specs lists registered specs; project-updates are callable
only by the project-structure extension and round-trip through the store +
broadcast the change. SDK client methods hit the right paths/payloads.

Run standalone:  python scripts/test_extension_sdk_capabilities.py
"""
from __future__ import annotations

import json
import os
import base64
import shutil
import sys
import tempfile
import urllib.request

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sdkcap-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
for _p in (_BACKEND, os.path.join(_REPO, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DIST_DIR = os.path.join(_REPO, "frontend", "dist")
_CREATED_DIST = not os.path.exists(_DIST_DIR)
if _CREATED_DIST:
    os.makedirs(_DIST_DIR, exist_ok=True)
    with open(os.path.join(_DIST_DIR, "index.html"), "w", encoding="utf-8") as _f:
        _f.write("<!doctype html><title>stub</title>")

from starlette.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import extension_store  # noqa: E402
import config_store  # noqa: E402
import provisioning  # noqa: E402
import project_update_store  # noqa: E402
import extension_token_registry  # noqa: E402
from paths import encode_cwd  # noqa: E402
from better_agent_sdk import Client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


CLIENT = TestClient(main.app, client=("127.0.0.1", 50002))
TOKEN = main.coordinator.internal_token
ACTIVE_EXT = "test.cap-ext"
INACTIVE_EXT = "test.cap-inactive"
STORAGE_EXT = "test.storage-ext"
FIELDS_EXT = "test.fields-ext"
WRITE_ONLY_FIELDS_EXT = "test.write-fields-ext"
PROJECT_STRUCTURE_EXT = extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID


def _seed(extension_id: str, *, enabled: bool, permissions: dict | None = None) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {"id": extension_id, "permissions": permissions or {}},
        "enabled": enabled,
        "source": {"type": "git", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)


def _configure_internal_llm_defaults(*tasks: str) -> None:
    providers = config_store.list_providers()["providers"]
    provider = providers[0]
    assignments = config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    config_store.set_internal_llm_assignments(assignments)


def _hdr(extension_id=ACTIVE_EXT):
    # Identity is token-derived: act as an extension by sending ITS minted token.
    return {"X-Internal-Token": extension_token_registry.mint(extension_id)}


class _FakeSpec(provisioning.ProvisionedSessionSpec):
    key = "test-cap-spec"
    name = "Capability test spec"


def main_test() -> int:
    _seed(ACTIVE_EXT, enabled=True)
    _seed(INACTIVE_EXT, enabled=False)
    _seed(STORAGE_EXT, enabled=True, permissions={"storage": True})
    _seed(FIELDS_EXT, enabled=True, permissions={"reads_session_fields": ["current_todos"]})
    _seed(WRITE_ONLY_FIELDS_EXT, enabled=True, permissions={"mutates_session_fields": ["current_todos"]})
    _seed(PROJECT_STRUCTURE_EXT, enabled=True)
    _configure_internal_llm_defaults("project_structure_edit")
    provisioning.register(_FakeSpec())

    print("C1 specs: token gate + lists registered specs")
    r = CLIENT.get("/api/internal/provisioned-sessions/specs")
    check(r.status_code in (403, 422), f"missing token rejected (got {r.status_code})")
    r = CLIENT.get(
        "/api/internal/provisioned-sessions/specs",
        headers={"X-Internal-Token": "wrong"},
    )
    check(r.status_code == 403, f"wrong token -> 403 (got {r.status_code})")
    r = CLIENT.get(
        "/api/internal/provisioned-sessions/specs",
        headers={"X-Internal-Token": TOKEN},
    )
    keys = [s["key"] for s in r.json().get("specs", [])]
    check(r.status_code == 200 and "test-cap-spec" in keys, f"lists registered spec (got {keys})")

    print("C2 broadcast-session: token + active extension + source pin")
    captured: dict = {}
    original_bs = main.coordinator.broadcast_session

    async def _fake_bs(app_session_id, event_type, data, *, source):
        captured.update(app_session_id=app_session_id, event_type=event_type, data=data, source=source)

    main.coordinator.broadcast_session = _fake_bs
    try:
        r = CLIENT.post("/api/internal/broadcast-session", json={})
        check(r.status_code in (403, 422), f"no token rejected (got {r.status_code})")
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"session_id": "s1", "event_type": "ext.evt", "data": {}},
            headers={"X-Internal-Token": TOKEN},  # no X-Extension-Id
        )
        check(r.status_code == 403, f"missing extension id -> 403 (got {r.status_code})")
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"session_id": "s1", "event_type": "ext.evt", "data": {}},
            headers=_hdr(INACTIVE_EXT),
        )
        check(r.status_code == 403, f"inactive extension -> 403 (got {r.status_code})")
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"event_type": "ext.evt", "data": {"x": 1}},  # missing session_id
            headers=_hdr(),
        )
        check(r.status_code == 400, f"missing session_id -> 400 (got {r.status_code})")
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"session_id": "s1", "event_type": "bad\nevent", "data": {}},
            headers=_hdr(),
        )
        check(r.status_code == 400, f"multiline event_type -> 400 (got {r.status_code})")
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"session_id": "s1", "event_type": "ext.evt", "data": [1, 2]},
            headers=_hdr(),
        )
        check(r.status_code == 400, f"non-object data -> 400 (got {r.status_code})")
        captured.clear()
        r = CLIENT.post(
            "/api/internal/broadcast-session",
            json={"session_id": "s1", "event_type": "ext.evt", "data": {"x": 1}},
            headers=_hdr(),
        )
        check(r.status_code == 200 and r.json().get("source") == f"extension:{ACTIVE_EXT}",
              "happy path pins source to extension id")
        check(captured.get("source") == f"extension:{ACTIVE_EXT}", "broadcast_session called with pinned source")
        check(captured.get("event_type") == "ext.evt" and captured.get("data") == {"x": 1},
              "broadcast_session forwarded event_type + data")
    finally:
        main.coordinator.broadcast_session = original_bs

    print("C3 session-fields: token + active extension + declared field gate")
    sess = session_manager.create(name="fields", model="m", cwd="/tmp", orchestration_mode="native")
    session_manager.set_current_todos(sess["id"], [{"content": "A", "status": "pending"}])
    r = CLIENT.post("/api/internal/session-fields", json={"session_id": sess["id"], "fields": ["current_todos"]})
    check(r.status_code in (403, 422), f"missing token rejected (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/session-fields",
        json={"session_id": sess["id"], "fields": ["current_todos"]},
        headers=_hdr(INACTIVE_EXT),
    )
    check(r.status_code == 403, f"inactive extension rejected (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/session-fields",
        json={"session_id": sess["id"], "fields": ["current_todos", "current_tasks"]},
        headers=_hdr(FIELDS_EXT),
    )
    body = r.json()
    check(
        r.status_code == 200
        and body.get("fields") == {"current_todos": [{"content": "A", "status": "pending"}]},
        f"returns only declared fields (got {body})",
    )
    r = CLIENT.post(
        "/api/internal/session-fields",
        json={"session_id": sess["id"], "fields": ["current_todos"]},
        headers=_hdr(WRITE_ONLY_FIELDS_EXT),
    )
    check(r.status_code == 200 and r.json().get("fields") == {}, "write permission does not grant field read")

    print("C3 project-updates list + mark-seen round-trip + broadcast")
    project_id = encode_cwd(_TMP_HOME)
    entry = project_update_store.append(project_id, "something changed")
    r = CLIENT.post(
        "/api/internal/project-updates/list",
        json={"cwd": _TMP_HOME},
        headers={"X-Internal-Token": TOKEN},
    )
    check(r.status_code == 403, f"project-updates rejects token-only caller (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/project-updates/list",
        json={"cwd": _TMP_HOME},
        headers=_hdr(PROJECT_STRUCTURE_EXT),
    )
    body = r.json()
    check(r.status_code == 200 and body["unseen_count"] == 1, f"list shows unseen (got {body})")
    check(any(u["id"] == entry["id"] for u in body["unseen_updates"]), "list includes captured entry")
    r = CLIENT.post(
        "/api/internal/project-updates/counts-batch",
        json={"cwds": [_TMP_HOME]},
        headers=_hdr(PROJECT_STRUCTURE_EXT),
    )
    body = r.json()
    check(
        r.status_code == 200 and body.get(project_id) == 1,
        f"counts-batch returns unseen count (got {body})",
    )
    r = CLIENT.post(
        "/api/internal/project-updates/list",
        json={"cwd": _TMP_HOME},
        headers={"X-Internal-Token": "wrong"},
    )
    check(r.status_code == 403, f"wrong token -> 403 (got {r.status_code})")

    broadcast_seen = {}
    original_bg = main.coordinator.broadcast_global

    async def _fake_bg(event_type, data):
        broadcast_seen.update(event_type=event_type, data=data)

    main.coordinator.broadcast_global = _fake_bg
    try:
        r = CLIENT.post(
            "/api/internal/project-updates/mark-seen",
            json={"cwd": _TMP_HOME, "entry_ids": [entry["id"]]},
            headers=_hdr(PROJECT_STRUCTURE_EXT),
        )
        check(r.status_code == 200 and r.json().get("marked") == 1, "mark-seen returns marked count")
        check(broadcast_seen.get("event_type") == "project_updates_changed", "mark-seen broadcasts change")
        check(project_update_store.unseen_count(project_id) == 0, "store now has 0 unseen")
        r = CLIENT.post(
            "/api/internal/project-updates/mark-seen",
            json={"cwd": _TMP_HOME, "entry_ids": "not-a-list"},
            headers=_hdr(PROJECT_STRUCTURE_EXT),
        )
        check(r.status_code == 400, f"bad entry_ids -> 400 (got {r.status_code})")
    finally:
        main.coordinator.broadcast_global = original_bg

    print("C4 extension storage: token + permission + path safety + round-trip")
    r = CLIENT.post(
        "/api/internal/extension-storage/put",
        json={"key": "state/cache.bin", "value_base64": base64.b64encode(b"abc").decode("ascii")},
        headers=_hdr(STORAGE_EXT),
    )
    check(r.status_code == 200 and r.json().get("size") == 3, f"put writes bytes (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/extension-storage/get",
        json={"key": "state/cache.bin"},
        headers=_hdr(STORAGE_EXT),
    )
    body = r.json()
    check(
        r.status_code == 200 and body.get("found") is True and base64.b64decode(body["value_base64"]) == b"abc",
        "get returns stored bytes",
    )
    r = CLIENT.post(
        "/api/internal/extension-storage/put",
        json={"key": "../escape", "value_base64": "YQ=="},
        headers=_hdr(STORAGE_EXT),
    )
    check(r.status_code == 400, f"traversal key -> 400 (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/extension-storage/get",
        json={"key": "state/cache.bin"},
        headers=_hdr(ACTIVE_EXT),
    )
    check(r.status_code == 403, f"extension without storage permission -> 403 (got {r.status_code})")
    r = CLIENT.post(
        "/api/internal/extension-storage/delete",
        json={"key": "state/cache.bin"},
        headers=_hdr(STORAGE_EXT),
    )
    check(r.status_code == 200 and r.json().get("deleted") is True, "delete removes stored bytes")

    print("C5 SDK methods hit the right paths/payloads")
    captured_req: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def _fake_urlopen(req, timeout=None):
        captured_req["method"] = req.get_method()
        captured_req["url"] = req.full_url
        captured_req["data"] = req.data.decode("utf-8") if req.data else ""
        return _FakeResp()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        c = Client(internal_token="tok", extension_id="ext-1", app_session_id="s1", backend_url="http://core")
        c.list_provisioned_specs()
        check(captured_req["method"] == "GET" and captured_req["url"].endswith("/api/internal/provisioned-sessions/specs"),
              f"list_provisioned_specs -> GET specs (got {captured_req['method']} {captured_req['url']})")
        c.broadcast_session_event("ext.evt", {"x": 1})
        body = json.loads(captured_req["data"])
        check(captured_req["url"].endswith("/api/internal/broadcast-session")
              and body == {"session_id": "s1", "event_type": "ext.evt", "data": {"x": 1}},
              "broadcast_session_event -> right path + payload")
        c.publish_session_event("ext.pub", {"y": 2})
        body = json.loads(captured_req["data"])
        check(captured_req["url"].endswith("/api/internal/broadcast-session")
              and body == {"session_id": "s1", "event_type": "ext.pub", "data": {"y": 2}},
              "publish_session_event aliases broadcast-session")
        c.storage_put("k", b"v")
        body = json.loads(captured_req["data"])
        check(captured_req["url"].endswith("/api/internal/extension-storage/put")
              and body == {"key": "k", "value_base64": "dg=="},
              "storage_put -> right path + base64 payload")
        c.storage_get("k")
        body = json.loads(captured_req["data"])
        check(captured_req["url"].endswith("/api/internal/extension-storage/get")
              and body == {"key": "k"},
              "storage_get -> right path + payload")
        c.storage_delete("k")
        body = json.loads(captured_req["data"])
        check(captured_req["url"].endswith("/api/internal/extension-storage/delete")
              and body == {"key": "k"},
              "storage_delete -> right path + payload")
    finally:
        urllib.request.urlopen = original_urlopen

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: extension sdk generic capability endpoints")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main_test())
    finally:
        if _CREATED_DIST:
            shutil.rmtree(_DIST_DIR, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
