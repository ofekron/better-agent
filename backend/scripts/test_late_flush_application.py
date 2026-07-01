"""Late-flush APPLICATION (dim8-F2 + hostile-review findings 2/3):
lines the turn-loop consumer will never read must not die in the
abandoned run queue. They must reach the orphan funnel —
`strategy.ingest_orphan` → events.jsonl with `msg_id=None` — so a later
read seq-brackets them onto the right msg. No WS subscriber, no Owned
tailer: the per-run tailer's own dispatch is the only producer in play.

Scenarios:
  1. linger      — post-`complete` flushes while the babysitter lingers
                   and at babysitter exit are applied, not just queued.
  2. cancel      — consumer exits on soft-cancel with lines still queued:
                   `release_queue` drains them through the orphan funnel
                   and flips the gate so later interrupt-drain lines go
                   orphan directly; nothing stranded.
  3. failed ingest — an orphan ingest that raises must propagate to the
                   tailer so the cursor does NOT advance past the line;
                   the tailer's dispatch retry then ingests it.

Run with:
    cd backend && .venv/bin/python scripts/test_late_flush_application.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lateflush-app-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import bus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from provider_claude import ClaudeProvider, RunState  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _FakePopen:
    def __init__(self):
        self.pid = os.getpid()
        self._rc = None

    def poll(self):
        return self._rc


def _line(uuid_: str, text: str, *, cli_sid: str) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": uuid_,
        "sessionId": cli_sid,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


def _mk_session(*, agent_sid: str, streaming: bool) -> tuple[str, str, str]:
    """Session whose primary agent sid is `agent_sid`, with one
    assistant msg — streaming (turn in flight) or finalized."""
    from orchs import get_strategy
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    session_manager.set_agent_sid(sid, "manager", agent_sid)
    scaffold = get_strategy("manager").build_assistant_scaffold()
    scaffold["isStreaming"] = True
    session_manager.append_assistant_msg(sid, scaffold)
    if not streaming:
        session_manager.set_streaming(sid, scaffold["id"], False)
    root_id = session_manager._root_id_for(sid) or sid
    return sid, root_id, scaffold["id"]


def _mk_prov() -> ClaudeProvider:
    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}
    prov.id = "test-prov"
    return prov


def _mk_run(prov, *, run_id: str, sid: str, agent_sid: str) -> RunState:
    """Real tailer + watcher topology, exactly like _bootstrap_run, with
    one in-turn line (u1) already in the jsonl."""
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    jsonl = run_dir / "session.jsonl"
    jsonl.write_text(_line("u1", "during-turn", cli_sid=agent_sid))
    rs = RunState(
        run_id=run_id,
        run_dir=run_dir,
        popen=_FakePopen(),
        mode="manager",
        app_session_id=sid,
        queue=asyncio.Queue(),
        persist_to=sid,
        session_id=agent_sid,
        jsonl_path=jsonl,
    )
    prov._runs[rs.run_id] = rs
    prov._start_tailer_and_watchers(rs, start_offset=0)
    return rs


def _append(rs: RunState, uuid_: str, text: str) -> int:
    with open(rs.jsonl_path, "a") as f:
        f.write(_line(uuid_, text, cli_sid=rs.session_id))
    return rs.jsonl_path.stat().st_size


async def _consume_to_complete(rs: RunState) -> None:
    """Play the turn loop: consume up to `complete`, then never again."""
    (rs.run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": rs.session_id, "error": None,
        "token_usage": None,
    }))
    while True:
        ev = await asyncio.wait_for(rs.queue.get(), timeout=5)
        if ev.type == "complete":
            return


async def _exit_and_deregister(prov, rs: RunState) -> bool:
    rs.popen._rc = 0
    return await _wait_for(
        lambda: rs.run_id not in prov._runs, timeout=10.0,
    )


def _journal_rows(root_id: str, sid: str) -> list[dict]:
    raw, _, _ = event_ingester.read_events(
        root_id, limit=10_000, sid_filter=sid,
    )
    return raw


def _orphan_row(rows: list[dict], uuid_: str):
    return next(
        (r for r in rows if (r.get("data") or {}).get("uuid") == uuid_),
        None,
    )


async def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


# ─── scenario 1: linger window (original dim8-F2) ─────────────────

async def _scenario_linger() -> None:
    print("scenario: linger window")
    agent_sid = "lfa-linger-sid"
    sid, root_id, _msg_id = _mk_session(agent_sid=agent_sid, streaming=False)
    prov = _mk_prov()
    rs = _mk_run(prov, run_id="run-linger", sid=sid, agent_sid=agent_sid)

    first = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(first.type == "agent_message", "in-turn line dispatched to queue")

    await _consume_to_complete(rs)
    check(prov._runs.get(rs.run_id) is rs, "run still registered (linger)")

    session_manager.consume_reconcile_dirty(root_id)

    # Late flush #1: CLI writes after the turn finalized, runner lingers.
    target = _append(rs, "u2", "late-flush-linger")
    advanced = await _wait_for(lambda: rs.processed_byte >= target)
    check(advanced, "tailer consumed the lingering late line")

    # Late flush #2 lands right before the babysitter exits — covered
    # by the _watch_linger_exit final drain.
    _append(rs, "u3", "late-flush-exit")
    check(await _exit_and_deregister(prov, rs),
          "run deregistered after process exit")

    # Journal writes are fire-and-forget — barrier before reading.
    event_journal_writer.barrier_sync(root_id)

    rows = _journal_rows(root_id, sid)
    u2 = _orphan_row(rows, "u2")
    u3 = _orphan_row(rows, "u3")
    check(u2 is not None, "lingering late flush landed in events.jsonl")
    check(u3 is not None, "exit-drain late flush landed in events.jsonl")
    check(u2 is not None and u2.get("msg_id") is None,
          "lingering late flush ingested as orphan (msg_id=None)")
    check(u3 is not None and u3.get("msg_id") is None,
          "exit-drain late flush ingested as orphan (msg_id=None)")
    check(session_manager.consume_reconcile_dirty(root_id),
          "reconcile-dirty armed so a later read brackets onto the msg")
    check(rs.queue.empty(),
          "no late events stranded in the abandoned run queue")


# ─── scenario 2: cancel path drains the queue ─────────────────────

async def _scenario_cancel_path_drains_queue() -> None:
    print("scenario: cancel path drains queue")
    agent_sid = "lfa-cancel-sid"
    # Mid-stream soft cancel: msg still streaming, no complete.json.
    sid, root_id, _msg_id = _mk_session(agent_sid=agent_sid, streaming=True)
    prov = _mk_prov()
    rs = _mk_run(prov, run_id="run-cancel", sid=sid, agent_sid=agent_sid)

    first = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(first.type == "agent_message", "in-turn line dispatched to queue")

    # Interrupt-drain lines pile up in the queue while the consumer is
    # already breaking out (cancel won the race).
    _append(rs, "a1", "cancel-tail-1")
    _append(rs, "a2", "cancel-tail-2")
    queued = await _wait_for(lambda: rs.queue.qsize() >= 2)
    check(queued, "cancel-tail lines queued, unconsumed")

    # Consumer exit (any reason — here: soft cancel). The turn loop's
    # finally hands the dead queue back to the provider.
    prov.release_queue(rs.run_id, rs.queue, persist_to=sid)
    check(rs.queue.empty(), "release drained the abandoned queue")
    check(rs.turn_finalized, "release flipped the late-flush gate")

    # A line flushed AFTER consumer exit (runner still draining ~15s)
    # must orphan-ingest directly, never touch the dead queue.
    target = _append(rs, "a3", "cancel-tail-post-exit")
    advanced = await _wait_for(lambda: rs.processed_byte >= target)
    check(advanced, "post-exit line consumed by tailer")
    check(rs.queue.empty(), "post-exit line never entered the dead queue")

    check(await _exit_and_deregister(prov, rs),
          "run deregistered after process exit")

    event_journal_writer.barrier_sync(root_id)
    rows = _journal_rows(root_id, sid)
    for uid in ("a1", "a2", "a3"):
        row = _orphan_row(rows, uid)
        check(row is not None and row.get("msg_id") is None,
              f"{uid} landed in events.jsonl as orphan")
    stranded = [
        ev for ev in getattr(rs.queue, "_queue", [])
        if getattr(ev, "type", None) == "agent_message"
    ]
    check(not stranded, "no agent_message stranded after exit")


# ─── scenario 2b: PREP consume loop (SubprocessAgent.init) ────────

async def _scenario_prep_cancel_drains_queue() -> None:
    print("scenario: prep cancel drains queue")
    from orchs._subprocess_agent import SubprocessAgent
    agent_sid = "lfa-prep-sid"
    sid, root_id, _msg_id = _mk_session(agent_sid=agent_sid, streaming=True)
    prov = _mk_prov()
    holder: dict = {}

    def _start_run(**kw):
        holder["start_run"] = kw
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
        jsonl = run_dir / "session.jsonl"
        jsonl.write_text("")
        rs = RunState(
            run_id=kw["run_id"],
            run_dir=run_dir,
            popen=_FakePopen(),
            mode=kw.get("mode") or "native",
            app_session_id=kw["app_session_id"],
            queue=kw["queue"],
            persist_to=kw["app_session_id"],
            session_id=agent_sid,
            jsonl_path=jsonl,
        )
        prov._runs[rs.run_id] = rs
        prov._start_tailer_and_watchers(rs, start_offset=0)
        holder["rs"] = rs

    prov.start_run = _start_run
    prov.cancel_turn = lambda run_id: True
    old_agent_url = os.environ.get("BETTER_AGENT_BACKEND_URL")
    old_claude_url = os.environ.get("BETTER_CLAUDE_BACKEND_URL")
    os.environ["BETTER_AGENT_BACKEND_URL"] = "http://127.0.0.1:8199"
    os.environ.pop("BETTER_CLAUDE_BACKEND_URL", None)

    class _Coord:
        internal_token = "prep-token"

        class turn_manager:
            current_assistant_msgs = {}

        def provider_for_session(self, _sid):
            return prov

        async def persist_and_dispatch_raw(self, _sid, evt):
            # The runner's interrupt-drain analog: lines flush into
            # the queue while the prep consumer is already exiting.
            if evt.get("type") == "agent_prep_cancelled":
                rs = holder["rs"]
                _append(rs, "p1", "prep-tail-1")
                _append(rs, "p2", "prep-tail-2")
                await _wait_for(lambda: rs.queue.qsize() >= 2)

    agent = SubprocessAgent(agent_session_id=sid, cwd="/tmp")
    cancel_event = asyncio.Event()
    cancel_event.set()
    try:
        res = await agent.init(
            _Coord(), model="sonnet", prep_prompt="x",
            cancel_event=cancel_event,
        )
    finally:
        if old_agent_url is None:
            os.environ.pop("BETTER_AGENT_BACKEND_URL", None)
        else:
            os.environ["BETTER_AGENT_BACKEND_URL"] = old_agent_url
        if old_claude_url is None:
            os.environ.pop("BETTER_CLAUDE_BACKEND_URL", None)
        else:
            os.environ["BETTER_CLAUDE_BACKEND_URL"] = old_claude_url
    rs = holder["rs"]
    check(res is None, "prep init returned None on cancel")
    check(holder["start_run"]["backend_url"] == "http://127.0.0.1:8199",
          "prep init forwards backend_url to provider")
    check(holder["start_run"]["internal_token"] == _Coord.internal_token,
          "prep init forwards internal_token to provider")
    check(rs.queue.empty(), "release drained the abandoned prep queue")
    check(rs.turn_finalized, "release flipped the late-flush gate")

    target = _append(rs, "p3", "prep-tail-post-exit")
    advanced = await _wait_for(lambda: rs.processed_byte >= target)
    check(advanced, "post-exit line consumed by tailer (cursor advanced)")
    check(rs.queue.empty(), "post-exit line never entered the dead queue")

    check(await _exit_and_deregister(prov, rs),
          "run deregistered after process exit")

    event_journal_writer.barrier_sync(root_id)
    rows = _journal_rows(root_id, sid)
    for uid in ("p1", "p2", "p3"):
        row = _orphan_row(rows, uid)
        check(row is not None and row.get("msg_id") is None,
              f"{uid} landed in events.jsonl as orphan")


# ─── scenario 3: failed orphan ingest blocks the cursor ───────────

async def _scenario_failed_orphan_ingest_blocks_cursor() -> None:
    print("scenario: failed orphan ingest blocks cursor")
    agent_sid = "lfa-fail-sid"
    sid, root_id, _msg_id = _mk_session(agent_sid=agent_sid, streaming=False)
    prov = _mk_prov()
    rs = _mk_run(prov, run_id="run-fail", sid=sid, agent_sid=agent_sid)

    await asyncio.wait_for(rs.queue.get(), timeout=5)
    await _consume_to_complete(rs)

    # Make the strategy's ingest_orphan raise twice, then succeed —
    # the provider must let the raise propagate so the tailer cursor
    # stays put and the dispatch retry re-ingests the SAME line.
    from orchs import get_strategy
    strategy = get_strategy("manager")
    orig = strategy.ingest_orphan
    calls = {"n": 0}

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("induced orphan-ingest failure")
        return orig(*a, **kw)

    strategy.ingest_orphan = _flaky
    try:
        before = rs.jsonl_path.stat().st_size
        target = _append(rs, "b1", "late-flush-flaky")
        attempted = await _wait_for(lambda: calls["n"] >= 1)
        check(attempted, "orphan ingest attempted")
        # First retry fires ~0.1s after the failure; success can't
        # come before the second backoff (>=0.7s) — sample inside
        # that window.
        await asyncio.sleep(0.25)
        check(rs.processed_byte == before,
              "cursor did NOT advance past the failed line")
        recovered = await _wait_for(lambda: rs.processed_byte >= target,
                                    timeout=10.0)
        check(recovered, "dispatch retry advanced the cursor")
    finally:
        strategy.ingest_orphan = orig

    check(calls["n"] >= 3, f"ingest retried (calls={calls['n']})")

    event_journal_writer.barrier_sync(root_id)
    rows = _journal_rows(root_id, sid)
    b1_rows = [
        r for r in rows if (r.get("data") or {}).get("uuid") == "b1"
    ]
    check(len(b1_rows) == 1 and b1_rows[0].get("msg_id") is None,
          f"retry ingested the line exactly once (rows={len(b1_rows)})")

    check(await _exit_and_deregister(prov, rs),
          "run deregistered after process exit")


async def _run_all() -> None:
    event_journal_writer.register(bus)
    await _scenario_linger()
    print()
    await _scenario_cancel_path_drains_queue()
    print()
    await _scenario_prep_cancel_drains_queue()
    print()
    await _scenario_failed_orphan_ingest_blocks_cursor()


def main() -> int:
    asyncio.run(_run_all())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: abandoned-queue lines are applied on every consumer exit path")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
