"""Backend regression test for the events.jsonl completeness contract.

Pins the invariants that make events.jsonl the durable, ordered log of
"everything that flowed into the BC backend":

(a) Every inbound state-mutating REST call to `/api/sessions/{sid}/...`
    is recorded as a `command_received` event BEFORE the handler runs.

(b) The claude jsonl tailer does NOT advance its cursor when
    `event_ingester.ingest` fails. The line is re-read on the next
    start, and `_seen_uuids` dedup makes the eventual ingest idempotent
    (no duplicates after restart).

(d) Offline resilience: a tailer started against a non-empty jsonl
    with cursor=0 ingests every line; restarting another tailer with
    cursor=0 ingests zero new lines (full dedup) and the file count
    is unchanged.

(e) `_ensure_open` recovers from a torn trailing line (the failure
    mode that follows a crash mid-`write` between fsync and newline,
    or any mid-write that leaves invalid JSON at EOF). Both variants:
    partial-JSON-no-newline AND complete-line-then-trailing-garbage.

(f) Structural guardrail: `broadcast_global` rejects any non-allowlist
    event type with `ValueError`; allowlisted types succeed.

(g) Per-session events (`supervisor_event`, `run_state`,
    `rewind_complete`) routed via `broadcast_session` actually land
    in events.jsonl with sid + payload.

Run with:
    cd backend && .venv/bin/python scripts/test_events_jsonl_completeness.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-completeness-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester, EventIngester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from jsonl_tailer import ClaudeJsonlTailer, OwnedClaudeJsonlTailer  # noqa: E402
from paths import ba_home  # noqa: E402
from auth_test_helpers import authenticate_client  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _events_path(root_id: str) -> Path:
    return ba_home() / "sessions" / root_id / "events.jsonl"


def _read_events(root_id: str) -> list[dict]:
    p = _events_path(root_id)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _seed_session(orch_mode: str = "native") -> str:
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp",
        orchestration_mode=orch_mode,
    )
    return sess["id"]


# ─── (a) REST command_received via FastAPI middleware ──────────────────
async def test_command_received_appears_in_events_jsonl() -> bool:
    """POST /api/sessions/{sid}/rename should write a `command_received`
    event into that session's events.jsonl, with the request payload
    captured under data.payload, before the handler runs."""
    # Import main only once we know BETTER_CLAUDE_HOME is set (already is).
    from fastapi.testclient import TestClient
    import main  # noqa: F401 — registers the middleware on the app

    sid = _seed_session()
    root_id = session_manager._root_id_for(sid) or sid

    client = TestClient(main.app, client=("127.0.0.1", 50000))
    authenticate_client(client)
    r = client.put(
        f"/api/sessions/{sid}/rename",
        json={"name": "renamed-by-test"},
    )
    if r.status_code >= 400:
        # 401 used to slip through the >=500 check; widen so an auth
        # regression or rename failure surfaces as a test failure
        # instead of a silent empty events.jsonl downstream.
        print(f"  rename returned {r.status_code}: {r.text[:200]}")
        return False

    events = _read_events(root_id)
    cmds = [e for e in events if e.get("type") == "command_received"]
    if not cmds:
        print(f"  no command_received in events.jsonl (saw {len(events)} events)")
        return False
    rec = cmds[-1]
    data = rec.get("data") or {}
    if data.get("method") != "PUT":
        print(f"  wrong method: {data.get('method')!r}")
        return False
    if not data.get("path", "").endswith("/rename"):
        print(f"  wrong path: {data.get('path')!r}")
        return False
    if rec.get("sid") != sid:
        print(f"  wrong sid: {rec.get('sid')!r}")
        return False
    if (data.get("payload") or {}).get("name") != "renamed-by-test":
        print(f"  payload not captured: {data.get('payload')!r}")
        return False
    if not data.get("uuid"):
        print("  uuid missing on command_received")
        return False
    return True


async def test_draft_autosave_skips_command_received() -> bool:
    from fastapi.testclient import TestClient
    import main  # noqa: F401

    sid = _seed_session()
    root_id = session_manager._root_id_for(sid) or sid

    client = TestClient(main.app, client=("127.0.0.1", 50000))
    authenticate_client(client)
    r = client.patch(
        f"/api/sessions/{sid}/draft",
        json={"draft_input": "typing", "client_seq": 1},
    )
    if r.status_code >= 400:
        print(f"  draft returned {r.status_code}: {r.text[:200]}")
        return False

    cmds = [e for e in _read_events(root_id) if e.get("type") == "command_received"]
    if cmds:
        print(f"  draft autosave wrote command_received: {cmds[-1]}")
        return False
    return True


# ─── (b) tailer durability: cursor stays on failure, idempotent retry ──
async def test_tailer_halts_on_dispatch_failure_and_dedupes() -> bool:
    sid = _seed_session()
    claude_sid = str(uuid.uuid4())
    jsonl = Path(_TMP_HOME) / "tailer_fail" / f"{claude_sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "x"}]},
    }
    jsonl.write_text(json.dumps(line) + "\n")

    root_id = session_manager._root_id_for(sid) or sid

    # Patch the per-instance dispatch to raise — bypass event_ingester
    # entirely so we test the tailer's cursor-on-success contract
    # without needing to fail-inject ingest itself.
    fail_count = {"n": 0}

    owned = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=claude_sid, jsonl_path=jsonl, start_offset=0,
    )

    real_dispatch = owned._dispatch

    def failing_dispatch(enriched: dict) -> None:
        fail_count["n"] += 1
        raise OSError("simulated ingest failure")

    owned._dispatch = failing_dispatch  # type: ignore[assignment]

    # Shrink retry backoff so the test halts in <2s instead of ~50s.
    from jsonl_tailer import JsonlEventTailer
    saved_backoff = JsonlEventTailer._DISPATCH_RETRY_BACKOFF
    JsonlEventTailer._DISPATCH_RETRY_BACKOFF = (0.01, 0.02, 0.03)

    try:
        owned.acquire()
        # Wait for the tailer to halt.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if owned._task is not None and owned._task.done():
                break
        if owned._task is None or not owned._task.done():
            print("  tailer did not halt in time")
            return False

        # Cursor must NOT be advanced past the failing line.
        sess = session_manager.get(sid)
        processed = (sess or {}).get("processed_line_by_sid", {}).get(claude_sid, 0)
        if processed != 0:
            print(f"  cursor advanced past failing line: processed={processed}")
            return False

        if fail_count["n"] < 3:
            print(f"  dispatch was not retried enough times: {fail_count['n']}")
            return False

        events_before = _read_events(root_id)
        n_before = len(events_before)

        # Restart with a fresh tailer + the REAL dispatch — line should
        # finally ingest, exactly once.
        owned2 = OwnedClaudeJsonlTailer(
            root_id=root_id, app_session_id=sid,
            agent_sid=claude_sid, jsonl_path=jsonl, start_offset=0,
        )
        owned2.acquire()
        # Wait for ingest to happen.
        for _ in range(100):
            await asyncio.sleep(0.05)
            evs = _read_events(root_id)
            if len(evs) > n_before:
                break
        events_after = _read_events(root_id)
        released = owned2.release()
        if released is not None:
            try:
                await released
            except Exception:
                pass

        line_uuid = line["uuid"]
        matching = [
            e for e in events_after
            if (e.get("data") or {}).get("uuid") == line_uuid
        ]
        if len(matching) != 1:
            print(f"  expected exactly 1 ingest of uuid {line_uuid[:8]}, "
                  f"got {len(matching)}")
            return False
        return True
    finally:
        JsonlEventTailer._DISPATCH_RETRY_BACKOFF = saved_backoff
        # Clean up the first (halted) tailer.
        if owned._tailer is not None:
            owned._tailer.stop()
        if owned._task is not None and not owned._task.done():
            try:
                await asyncio.wait_for(owned._task, timeout=1)
            except (asyncio.TimeoutError, Exception):
                pass


# ─── (d) offline resilience + restart-dedup ────────────────────────────
async def test_offline_catchup_and_restart_dedup() -> bool:
    sid = _seed_session()
    claude_sid = str(uuid.uuid4())
    jsonl = Path(_TMP_HOME) / "offline" / f"{claude_sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for _ in range(5):
        lines.append({
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "x"}]},
        })
    jsonl.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    root_id = session_manager._root_id_for(sid) or sid

    owned = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=claude_sid, jsonl_path=jsonl, start_offset=0,
    )
    owned.acquire()

    line_uuids = {l["uuid"] for l in lines}

    async def wait_for_count(target: int) -> bool:
        for _ in range(200):
            await asyncio.sleep(0.05)
            seen = {
                (e.get("data") or {}).get("uuid")
                for e in _read_events(root_id)
            } & line_uuids
            if len(seen) >= target:
                return True
        return False

    if not await wait_for_count(5):
        print(f"  tailer never caught up to 5 lines")
        return False

    events_after_first = _read_events(root_id)
    first_count = len(events_after_first)

    # Stop first tailer cleanly.
    released = owned.release()
    if released is not None:
        try:
            await released
        except Exception:
            pass

    # Restart with cursor=0 — every line is already in dedup set, so
    # zero new rows.
    owned2 = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=claude_sid, jsonl_path=jsonl, start_offset=0,
    )
    owned2.acquire()
    # Give the tailer time to read all 5 lines and attempt-dedup each.
    await asyncio.sleep(0.5)
    released = owned2.release()
    if released is not None:
        try:
            await released
        except Exception:
            pass

    events_after_second = _read_events(root_id)
    # Invariant under test: each of the 5 line-uuids appears exactly
    # once in events.jsonl, both before and after the restart-with-
    # cursor=0. Unrelated events (e.g. command_received from earlier
    # REST tests against this session) don't affect this dedup check.
    def line_count(evs: list[dict]) -> int:
        return sum(
            1 for e in evs
            if (e.get("data") or {}).get("uuid") in line_uuids
        )
    if line_count(events_after_first) != 5:
        print(f"  first-pass line uuids: "
              f"{line_count(events_after_first)}/5")
        return False
    if line_count(events_after_second) != 5:
        print(f"  restart duplicated line uuids: "
              f"{line_count(events_after_second)}/5 expected")
        return False
    return True


async def test_claude_tailer_rewinds_oversized_cursor() -> bool:
    jsonl = Path(_TMP_HOME) / "rewind" / f"{uuid.uuid4()}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "rewound"}]},
    }
    raw = json.dumps(line) + "\n"
    jsonl.write_text(raw, encoding="utf-8")

    seen: list[dict] = []
    cursors: list[int] = []
    tailer = ClaudeJsonlTailer(
        path=jsonl,
        start_offset=len(raw) + 1000,
        dispatch=seen.append,
        on_cursor_advance=cursors.append,
    )
    task = asyncio.create_task(tailer.run())
    try:
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.05)
    finally:
        tailer.stop()
        await task

    if not seen:
        print("  oversized cursor did not rewind/read current file")
        return False
    if seen[0].get("uuid") != line["uuid"]:
        print(f"  wrong event after rewind: {seen[0]!r}")
        return False
    if not cursors or cursors[0] != 0:
        print(f"  cursor reset was not reported first: {cursors!r}")
        return False
    if cursors[-1] != len(raw):
        print(f"  cursor did not advance to current file size: {cursors!r}")
        return False

    owned = OwnedClaudeJsonlTailer(
        root_id="root",
        app_session_id="app",
        agent_sid="agent",
        jsonl_path=jsonl,
        start_offset=len(raw) + 1000,
    )
    persisted: list[int] = []
    owned._persist_cursor = persisted.append  # type: ignore[method-assign]
    owned._on_cursor(0)
    if persisted != [0]:
        print(f"  owned cursor did not persist reset: {persisted!r}")
        return False
    return True


# ─── (e) torn-tail recovery ────────────────────────────────────────────
async def test_torn_tail_recovery_both_variants() -> bool:
    # Use a fresh ingester to bypass the singleton's open handle cache.
    ing = EventIngester()
    root_id = f"torn-root-{uuid.uuid4()}"
    sid = "test-sid"
    # Write one valid event, then corrupt the tail.
    seq = ing.ingest(
        root_id, sid=sid, event_type="agent_message",
        data={"uuid": str(uuid.uuid4()), "n": 1}, source="test",
    )
    if seq != 1:
        print(f"  unexpected first seq: {seq}")
        return False
    ing.close(root_id)

    path = ba_home() / "sessions" / root_id / "events.jsonl"
    # Variant 1: append a partial JSON line WITHOUT a trailing newline
    # (simulates crash mid-write).
    with open(path, "ab") as f:
        f.write(b'{"seq":2,"sid":"x","type":"agent_message","data":{"uu')

    # Reopen via a fresh ingester to trigger _ensure_open recovery.
    ing2 = EventIngester()
    ing2.ingest(
        root_id, sid=sid, event_type="agent_message",
        data={"uuid": str(uuid.uuid4()), "n": 2}, source="test",
    )
    events = _read_events(root_id)
    if len(events) != 2:
        print(f"  variant 1: expected 2 events, got {len(events)}; "
              f"file may still have the torn line in middle")
        return False
    # Last appended event should have seq=2 (existing_lines=1 after
    # truncation → next seq = 2).
    if events[-1].get("seq") != 2:
        print(f"  variant 1: appended seq mismatch: "
              f"{events[-1].get('seq')}")
        return False
    ing2.close(root_id)

    # Variant 2: append a complete-but-garbage trailing line. _ensure_open
    # must recognize it as un-parseable and truncate it too.
    with open(path, "ab") as f:
        f.write(b'not json at all\n')

    ing3 = EventIngester()
    ing3.ingest(
        root_id, sid=sid, event_type="agent_message",
        data={"uuid": str(uuid.uuid4()), "n": 3}, source="test",
    )
    events = _read_events(root_id)
    if len(events) != 3:
        print(f"  variant 2: expected 3 events, got {len(events)}")
        return False
    if events[-1].get("seq") != 3:
        print(f"  variant 2: appended seq mismatch: "
              f"{events[-1].get('seq')}")
        return False
    ing3.close(root_id)
    return True


# ─── (f) guardrail: broadcast_global allowlist enforced ────────────────
async def test_broadcast_global_allowlist_enforced() -> bool:
    coordinator = Coordinator()
    # Allowlisted should succeed (no subscribers; no-op).
    try:
        await coordinator.broadcast_global("provider_changed", {})
    except Exception as e:
        print(f"  allowlisted call raised: {e}")
        return False
    # Non-allowlisted must raise ValueError.
    try:
        await coordinator.broadcast_global("supervisor_event", {})
    except ValueError:
        pass
    except Exception as e:
        print(f"  expected ValueError, got {type(e).__name__}: {e}")
        return False
    else:
        print("  non-allowlisted call did NOT raise")
        return False
    return True


async def test_broadcast_global_does_not_wait_for_slow_client() -> bool:
    coordinator = Coordinator()
    release_slow = asyncio.Event()
    slow_started = asyncio.Event()
    fast_seen: list[dict] = []

    async def slow_cb(event: dict) -> None:
        slow_started.set()
        await release_slow.wait()

    async def fast_cb(event: dict) -> None:
        fast_seen.append(event)

    coordinator.register_global_ws(slow_cb)
    coordinator.register_global_ws(fast_cb)

    t0 = time.perf_counter()
    await coordinator.broadcast_global("provider_changed", {"x": 1})
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if elapsed_ms > 50.0:
        print(f"  broadcast_global waited {elapsed_ms:.1f}ms for callbacks")
        release_slow.set()
        await asyncio.sleep(0)
        return False

    for _ in range(10):
        if fast_seen and slow_started.is_set():
            break
        await asyncio.sleep(0)
    release_slow.set()
    await asyncio.sleep(0)

    if not fast_seen:
        print("  fast callback did not receive broadcast")
        return False
    if fast_seen[-1].get("type") != "provider_changed":
        print(f"  wrong event: {fast_seen[-1]!r}")
        return False
    return True


# ─── (g) broadcast_session ingests per-session events ──────────────────
async def test_broadcast_session_persists_event() -> bool:
    sid = _seed_session()
    root_id = session_manager._root_id_for(sid) or sid

    coordinator = Coordinator()
    test_uuid = str(uuid.uuid4())
    await coordinator.broadcast_session(
        sid, "supervisor_event",
        {"session_id": sid, "kind": "verdict_failed",
         "uuid": test_uuid, "error": "synthetic"},
        source="test.supervisor",
    )
    # And run_state / rewind_complete via the same funnel.
    await coordinator.broadcast_session(
        sid, "run_state",
        {"app_session_id": sid, "runs": [],
         "uuid": str(uuid.uuid4())},
        source="test.run_state",
    )
    await coordinator.broadcast_session(
        sid, "rewind_complete",
        {"session_id": sid, "messages": [],
         "uuid": str(uuid.uuid4())},
        source="test.rewind",
    )
    events = _read_events(root_id)
    types = {e.get("type") for e in events}
    for t in ("supervisor_event", "run_state", "rewind_complete"):
        if t not in types:
            print(f"  {t} missing from events.jsonl (saw {types})")
            return False
    # Sid stamped at top level on every entry.
    for e in events:
        if e.get("type") in {
            "supervisor_event", "run_state", "rewind_complete",
        } and e.get("sid") != sid:
            print(f"  {e.get('type')} entry has wrong sid: {e.get('sid')!r}")
            return False
    return True


async def test_broadcast_session_ignores_closed_journal_error() -> bool:
    import event_journal

    sid = _seed_session()
    original = event_journal.publish_event
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    async def closed_writer(**_kwargs: object) -> int:
        raise event_journal.EventJournalWriteError("event journal writer is closed")

    handler = Capture()
    logger = logging.getLogger("orchestrator")
    logger.addHandler(handler)
    event_journal.publish_event = closed_writer
    try:
        await Coordinator().broadcast_session(
            sid,
            "run_state",
            {"app_session_id": sid, "runs": []},
            source="test.closed_journal",
        )
    finally:
        event_journal.publish_event = original
        logger.removeHandler(handler)

    errors = [record for record in records if record.levelno >= logging.ERROR]
    if errors:
        print(f"  closed writer logged error: {errors[-1].getMessage()}")
        return False
    return True


async def test_broadcast_session_logs_other_journal_errors() -> bool:
    import event_journal

    sid = _seed_session()
    original = event_journal.publish_event
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    async def broken_writer(**_kwargs: object) -> int:
        raise event_journal.EventJournalWriteError("simulated journal failure")

    handler = Capture()
    logger = logging.getLogger("orchestrator")
    logger.addHandler(handler)
    event_journal.publish_event = broken_writer
    try:
        await Coordinator().broadcast_session(
            sid,
            "run_state",
            {"app_session_id": sid, "runs": []},
            source="test.journal_failure",
        )
    finally:
        event_journal.publish_event = original
        logger.removeHandler(handler)

    errors = [record for record in records if record.levelno >= logging.ERROR]
    if not errors:
        print("  non-closed journal failure did not log an error")
        return False
    return True


async def test_broadcast_session_uses_async_journal_without_sync_timeout() -> bool:
    import event_journal

    sid = _seed_session()
    root_id = session_manager._root_id_for(sid) or sid
    test_uuid = str(uuid.uuid4())
    original_async = event_journal.event_journal_writer.submit_event_async
    original_sync = event_journal.publish_event_sync
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    async def delayed_async(event):
        await asyncio.sleep(0.05)
        return await original_async(event)

    def forbidden_sync(**_kwargs: object) -> int:
        raise TimeoutError("sync journal timeout path used")

    handler = Capture()
    logger = logging.getLogger("orchestrator")
    logger.addHandler(handler)
    event_journal.event_journal_writer.submit_event_async = delayed_async
    event_journal.publish_event_sync = forbidden_sync
    try:
        start = time.perf_counter()
        await Coordinator().broadcast_session(
            sid,
            "run_state",
            {"app_session_id": sid, "runs": [], "uuid": test_uuid},
            source="test.async_journal",
        )
        elapsed = time.perf_counter() - start
    finally:
        event_journal.event_journal_writer.submit_event_async = original_async
        event_journal.publish_event_sync = original_sync
        logger.removeHandler(handler)

    errors = [record for record in records if record.levelno >= logging.ERROR]
    if errors:
        print(f"  async journal path logged error: {errors[-1].getMessage()}")
        return False
    if elapsed < 0.04:
        print("  async journal path returned before delayed write completed")
        return False
    events = _read_events(root_id)
    if not any((e.get("data") or {}).get("uuid") == test_uuid for e in events):
        print("  async journal path did not persist delayed run_state")
        return False
    return True


TESTS = [
    ("(a) REST command_received persists into events.jsonl",
     test_command_received_appears_in_events_jsonl),
    ("(a2) draft autosave stays off command journal",
     test_draft_autosave_skips_command_received),
    ("(b) tailer halts on dispatch fail; uuid dedup prevents duplicates",
     test_tailer_halts_on_dispatch_failure_and_dedupes),
    ("(d) offline catch-up + restart dedup",
     test_offline_catchup_and_restart_dedup),
    ("(d2) Claude tailer rewinds oversized persisted byte cursor",
     test_claude_tailer_rewinds_oversized_cursor),
    ("(e) torn-tail recovery (both variants)",
     test_torn_tail_recovery_both_variants),
    ("(f) broadcast_global allowlist enforced",
     test_broadcast_global_allowlist_enforced),
    ("(f2) broadcast_global does not wait for slow WS clients",
     test_broadcast_global_does_not_wait_for_slow_client),
    ("(g) broadcast_session persists supervisor/run_state/rewind events",
     test_broadcast_session_persists_event),
    ("(g2) broadcast_session ignores closed journal during shutdown",
     test_broadcast_session_ignores_closed_journal_error),
    ("(g3) broadcast_session still logs non-shutdown journal failures",
     test_broadcast_session_logs_other_journal_errors),
    ("(g4) broadcast_session uses async journal without sync timeout",
     test_broadcast_session_uses_async_journal_without_sync_timeout),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = asyncio.run(fn())
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        try:
            event_ingester.close_all()
        except Exception:
            pass
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
