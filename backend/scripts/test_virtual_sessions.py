from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-virtual-sessions-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_SDK = os.path.dirname(_BACKEND) + "/sdk"
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

from fastapi.testclient import TestClient  # noqa: E402

from better_agent_sdk.client import Client as SdkClient  # noqa: E402
import extension_store  # noqa: E402
import auth  # noqa: E402
import extension_session_ownership  # noqa: E402
import main  # noqa: E402
import synthetic_messages  # noqa: E402
import virtual_session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _headers(extension_id: str | None = None) -> dict[str, str]:
    # Identity is token-derived: act as an extension by sending ITS minted
    # token; without an extension, use the core token (no extension principal).
    import extension_token_registry
    token = (
        extension_token_registry.mint(extension_id)
        if extension_id
        else main.coordinator.internal_token
    )
    return {"X-Internal-Token": token}


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.create_token('test')}"}


def _install_extension_with_session_state(extension_id: str) -> None:
    package = Path(_TMP_HOME) / "extension-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {"session_state": True},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
    )


def test_store_owner_and_projection_shape() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:test"
    session = virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Projected",
            "cwd": "/tmp/project",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    if session.get("virtual") is not True:
        print("  virtual flag missing")
        return False
    if session.get("orchestration_mode") != "virtual":
        print(f"  expected virtual orchestration mode, got {session.get('orchestration_mode')!r}")
        return False
    if session.get("message_count") != 1:
        print(f"  expected message_count=1, got {session.get('message_count')!r}")
        return False
    try:
        virtual_session_store.upsert("other-extension", {"id": sid, "name": "bad"})
    except ValueError:
        return True
    print("  non-owner upsert should fail")
    return False


def test_messages_are_computed_from_backing_and_synthetic() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    real = session_manager.create(name="backing", model="model", cwd="/tmp/project")
    extension_session_ownership.claim(real["id"], ext)
    session_manager.append_user_msg(
        real["id"],
        {
            "id": "real-user",
            "role": "user",
            "content": "from real session",
            "timestamp": "2026-01-01T00:00:00",
            "events": [],
            "isStreaming": False,
        },
    )
    sid = f"virtual:{ext}:computed"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Computed",
            "backing_session_ids": [real["id"]],
            "synthetic_messages": [
                {
                    "id": "synthetic-user",
                    "role": "assistant",
                    "content": "from synthetic",
                    "timestamp": "2026-01-01T00:00:01",
                }
            ],
        },
    )
    loaded = virtual_session_store.get(sid) or {}
    messages = loaded.get("messages") or []
    contents = [m.get("content") for m in messages]
    if contents != ["from real session", "from synthetic"]:
        print(f"  expected computed messages, got {contents!r}")
        return False
    if messages[0].get("backing_session_id") != real["id"]:
        print("  backing message not stamped with source session")
        return False
    return True


def test_unowned_backing_session_is_rejected() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    real = session_manager.create(name="private", model="model", cwd="/tmp/project")
    try:
        virtual_session_store.upsert(
            ext,
            {
                "id": f"virtual:{ext}:unowned-backing",
                "name": "Bad backing",
                "backing_session_ids": [real["id"]],
            },
        )
    except PermissionError as exc:
        return "backing session" in str(exc)
    print("  unowned backing session was accepted")
    return False


def test_internal_api_lists_and_loads_virtual_session() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    _install_extension_with_session_state(ext)
    sid = f"virtual:{ext}:api"
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    response = client.post(
        "/api/internal/virtual-sessions/upsert",
        headers=_headers(ext),
        json={
            "id": sid,
            "name": "API projection",
            "cwd": "/tmp/project",
            "messages": [{"role": "assistant", "content": "projected answer"}],
        },
    )
    if response.status_code != 200:
        print(f"  upsert failed: {response.status_code} {response.text}")
        return False
    listed = client.get("/api/sessions", headers=_api_headers()).json().get("sessions") or []
    summary = next((s for s in listed if s.get("id") == sid), None)
    if summary is None:
        print("  virtual session missing from /api/sessions")
        return False
    if "messages" in summary:
        print("  virtual session summary should not include full messages")
        return False
    loaded = client.get(f"/api/sessions/{sid}", headers=_api_headers()).json()
    if loaded.get("id") != sid:
        print(f"  load returned wrong id: {loaded.get('id')!r}")
        return False
    if (loaded.get("messages") or [{}])[0].get("content") != "projected answer":
        print("  projected message missing from loaded session")
        return False
    return True


def test_metadata_size_is_bounded() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    try:
        virtual_session_store.upsert(
            ext,
            {
                "id": f"virtual:{ext}:huge-metadata",
                "name": "Huge",
                "metadata": {"blob": "x" * (70 * 1024)},
            },
        )
    except ValueError as exc:
        return "metadata exceeds" in str(exc)
    print("  oversized metadata was accepted")
    return False


def test_concurrent_appends_are_not_lost() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:concurrent"
    virtual_session_store.upsert(ext, {"id": sid, "name": "Concurrent"})

    def append(i: int) -> None:
        virtual_session_store.append_message(
            ext,
            sid,
            {"id": f"m-{i}", "role": "user", "content": str(i)},
        )

    threads = [threading.Thread(target=append, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    messages = (virtual_session_store.get(sid) or {}).get("messages") or []
    got = {m.get("id") for m in messages}
    want = {f"m-{i}" for i in range(24)}
    missing = want - got
    if missing:
        print(f"  lost messages: {sorted(missing)!r}")
        return False
    return True


def test_list_all_cache_isolated_and_invalidated() -> bool:
    source = open(virtual_session_store.__file__, "r", encoding="utf-8").read()
    list_start = source.index("def _list_summaries(")
    list_end = source.index("def get(", list_start)
    list_source = source[list_start:list_end]
    for timer in (
        "virtual_sessions.list.load",
        "virtual_sessions.list.copy_cached",
        "virtual_sessions.list.project",
        "virtual_sessions.list.sort",
        "virtual_sessions.list.cache_copy",
        "virtual_sessions.list.copy_result",
    ):
        if timer not in list_source:
            print(f"  missing virtual list timer {timer}")
            return False

    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:cached-list"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Cached list",
            "metadata": {"nested": {"count": 1}},
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    first = virtual_session_store.list_all()
    first_summary = next((session for session in first if session.get("id") == sid), None)
    if first_summary is None:
        print("  cached virtual session missing from first list")
        return False
    first_summary["metadata"]["nested"]["count"] = 99
    second = virtual_session_store.list_all()
    second_summary = next((session for session in second if session.get("id") == sid), None)
    if second_summary is None:
        print("  cached virtual session missing from second list")
        return False
    if second_summary.get("metadata", {}).get("nested", {}).get("count") != 1:
        print(f"  caller mutation leaked into cache: {second_summary!r}")
        return False
    virtual_session_store.append_message(
        ext,
        sid,
        {"id": "m-2", "role": "assistant", "content": "two"},
    )
    third = virtual_session_store.list_all()
    third_summary = next((session for session in third if session.get("id") == sid), None)
    if third_summary is None:
        print("  cached virtual session missing after append")
        return False
    if third_summary.get("message_count") != 2:
        print(f"  cache was not invalidated after append: {third_summary!r}")
        return False
    return True


def test_list_recent_copies_only_requested_summaries() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    for index in range(4):
        virtual_session_store.upsert(
            ext,
            {
                "id": f"virtual:{ext}:recent-{index}",
                "name": f"Recent {index}",
                "metadata": {"nested": {"count": index}},
                "messages": [{"id": f"m-{index}", "role": "user", "content": "one"}],
            },
        )
    first, total = virtual_session_store.list_recent(2)
    if len(first) != 2:
        print(f"  expected bounded recent list, got {len(first)}")
        return False
    if total < 4:
        print(f"  expected total to include omitted rows, got {total}")
        return False
    first[0]["metadata"]["nested"]["count"] = 99
    second, _ = virtual_session_store.list_recent(2)
    if second[0].get("metadata", {}).get("nested", {}).get("count") == 99:
        print("  caller mutation leaked through list_recent")
        return False
    excluded, excluded_total = virtual_session_store.list_recent(
        10,
        exclude_id=first[0].get("id"),
    )
    if any(session.get("id") == first[0].get("id") for session in excluded):
        print("  excluded id returned from list_recent")
        return False
    if excluded_total != total - 1:
        print(f"  excluded total mismatch: total={total} excluded={excluded_total}")
        return False
    return True


def test_list_all_summary_cache_skips_full_payload_copy() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:summary-hot-path"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Summary hot path",
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    if not any(session.get("id") == sid for session in virtual_session_store.list_all()):
        print("  virtual session missing before cache test")
        return False
    original = virtual_session_store.deepcopy

    def guarded_deepcopy(value):
        if isinstance(value, dict) and isinstance(value.get("sessions"), dict):
            raise AssertionError("list_all copied full virtual session store")
        return original(value)

    virtual_session_store.deepcopy = guarded_deepcopy
    try:
        cached = virtual_session_store.list_all()
    finally:
        virtual_session_store.deepcopy = original
    return any(session.get("id") == sid for session in cached)


def test_cold_list_all_skips_full_payload_copy() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:cold-summary-path"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Cold summary path",
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    virtual_session_store._cache_signature = None
    virtual_session_store._cache_data = None
    virtual_session_store._summary_cache_signature = None
    virtual_session_store._summary_cache = None
    virtual_session_store._summary_cache_fresh_until = 0.0
    original = virtual_session_store.deepcopy

    def guarded_deepcopy(value):
        if isinstance(value, dict) and isinstance(value.get("sessions"), dict):
            raise AssertionError("cold list_all copied full virtual session store")
        return original(value)

    virtual_session_store.deepcopy = guarded_deepcopy
    try:
        listed = virtual_session_store.list_all()
    finally:
        virtual_session_store.deepcopy = original
    return any(session.get("id") == sid for session in listed)


def test_list_all_hot_cache_skips_store_load() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:hot-cache"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Hot cache",
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    if not any(session.get("id") == sid for session in virtual_session_store.list_all()):
        print("  virtual session missing before hot-cache test")
        return False
    original = virtual_session_store._load_shared_locked

    def fail_load():
        raise AssertionError("hot cached list_all touched virtual session store")

    virtual_session_store._load_shared_locked = fail_load
    try:
        cached = virtual_session_store.list_all()
    finally:
        virtual_session_store._load_shared_locked = original
    return any(session.get("id") == sid for session in cached)


def test_list_all_returns_cached_projection_when_store_lock_busy() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:busy-lock"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Busy lock",
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    if not any(session.get("id") == sid for session in virtual_session_store.list_all()):
        print("  virtual session missing before busy-lock test")
        return False
    if not virtual_session_store._lock.acquire(blocking=False):
        print("  virtual store lock unexpectedly busy before test")
        return False
    try:
        started = time.perf_counter()
        cached = virtual_session_store.list_all()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    finally:
        virtual_session_store._lock.release()
    if elapsed_ms > 50.0:
        print(f"  cached list waited behind busy lock: {elapsed_ms:.2f}ms")
        return False
    return any(session.get("id") == sid for session in cached)


def test_write_warms_summary_cache_for_recent_list() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    sid = f"virtual:{ext}:write-warm-cache"
    virtual_session_store.upsert(
        ext,
        {
            "id": sid,
            "name": "Write warm cache",
            "messages": [{"id": "m-1", "role": "user", "content": "one"}],
        },
    )
    original = virtual_session_store._load_shared_locked

    def fail_load():
        raise AssertionError("list_recent_cached touched virtual session store after write")

    virtual_session_store._load_shared_locked = fail_load
    try:
        cached = virtual_session_store.list_recent_cached(10)
    finally:
        virtual_session_store._load_shared_locked = original
    if cached is None:
        print("  write did not warm virtual summary cache")
        return False
    sessions, _total = cached
    return any(session.get("id") == sid for session in sessions)


def test_sdk_namespaces_short_virtual_ids_for_all_methods() -> bool:
    ext = extension_store.BUILTIN_ASK_EXTENSION_ID
    client = SdkClient(extension_id=ext, internal_token="token")
    calls: list[tuple[str, dict]] = []

    def fake_post(path, payload, *, timeout=60.0):
        calls.append((path, payload))
        return {"success": True}

    client._post = fake_post  # type: ignore[method-assign]
    client.upsert_virtual_session("short", name="Short")
    client.append_virtual_session_message("short", "user", "hello")
    client.delete_virtual_session("short")
    ids = [
        calls[0][1].get("id"),
        calls[1][1].get("session_id"),
        calls[2][1].get("session_id"),
    ]
    want = f"virtual:{ext}:short"
    if ids != [want, want, want]:
        print(f"  expected all SDK ids to be {want!r}, got {ids!r}")
        return False
    return True


def test_internal_api_rejects_extension_without_session_state() -> bool:
    client = TestClient(main.app, client=("127.0.0.1", 50001))
    response = client.post(
        "/api/internal/virtual-sessions/upsert",
        headers=_headers(),
        json={"id": "virtual:missing:nope", "name": "Nope"},
    )
    if response.status_code != 403:
        print(f"  expected 403, got {response.status_code}: {response.text}")
        return False
    return True


def test_synthetic_injection_queues_normal_turn() -> bool:
    session = session_manager.create(name="real", model="model", cwd="/tmp")
    captured: dict = {}

    class FakeTurnManager:
        def has_active_turn(self, _sid):
            return False

        def has_active_runs(self, _sid):
            return False

    class FakeCoordinator:
        turn_manager = FakeTurnManager()

        def get_queued_count(self, _sid):
            return 0

        async def submit_prompt_async(self, sid, params):
            captured["sid"] = sid
            captured["params"] = params

    result = asyncio.run(
        synthetic_messages.inject(
            FakeCoordinator(),
            session["id"],
            prompt="internal prompt",
            display_prompt="visible prompt",
            source="synthetic-test",
        )
    )
    if result.get("success") is not True:
        print(f"  injection failed: {result}")
        return False
    queued = (session_manager.get(session["id"]) or {}).get("queued_prompts") or []
    if not queued or queued[0].get("content") != "visible prompt":
        print(f"  queued prompt not persisted correctly: {queued!r}")
        return False
    if captured.get("params", {}).get("cli_prompt") != "internal prompt":
        print(f"  cli_prompt not passed to coordinator: {captured!r}")
        return False
    return True


TESTS = [
    ("store owner + projection shape", test_store_owner_and_projection_shape),
    ("messages computed from backing + synthetic", test_messages_are_computed_from_backing_and_synthetic),
    ("unowned backing session is rejected", test_unowned_backing_session_is_rejected),
    ("internal API lists + loads virtual session", test_internal_api_lists_and_loads_virtual_session),
    ("metadata size is bounded", test_metadata_size_is_bounded),
    ("concurrent appends are not lost", test_concurrent_appends_are_not_lost),
    ("list_all cache is isolated + invalidated", test_list_all_cache_isolated_and_invalidated),
    ("list_all summary cache skips full payload copy", test_list_all_summary_cache_skips_full_payload_copy),
    ("cold list_all skips full payload copy", test_cold_list_all_skips_full_payload_copy),
    ("list_all hot cache skips store load", test_list_all_hot_cache_skips_store_load),
    ("list_all returns cached projection when store lock busy", test_list_all_returns_cached_projection_when_store_lock_busy),
    ("write warms summary cache for recent list", test_write_warms_summary_cache_for_recent_list),
    ("SDK namespaces short virtual ids for all methods", test_sdk_namespaces_short_virtual_ids_for_all_methods),
    ("internal API rejects extension without session_state", test_internal_api_rejects_extension_without_session_state),
    ("synthetic injection queues normal turn", test_synthetic_injection_queues_normal_turn),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            ok = False
            try:
                ok = fn()
            except Exception as exc:
                print(f"  exception: {exc}")
            print(f"{PASS if ok else FAIL} {name}")
            if not ok:
                failed += 1
        return 1 if failed else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
