"""Root-cause regression for the live-validation field defect (zero
assistant `node_upsert` frames on a real `/ws/v2/surface` haiku turn —
see `DIAGNOSIS.md`/`decisive-trace.json` evidence sets: `chat_adapter.
flush` shows `rows=1` on every tick for the whole turn duration yet
`upserts=0 text_deltas=0` throughout, and the ONLY `node_upsert` frame
the WS ever carries is the user's own `typed_prompt`).

Every existing test touching this area either (a) stubs `_drive_cli_run`
entirely (`test_v2_projection_end_to_end.py::test_real_dispatch_through_
turn_manager_run_turn_journals_prompt` — proves the prompt-row write call
site fires, proves NOTHING about provider-stream content) or (b) hand-
constructs journal rows directly via `event_ingester.ingest` — proves the
READ side given a row someone else already wrote, proves NOTHING about
whether `run_turn`'s real queue-driven write call site ever produces that
row. Neither exercises the actual emit chain: `provider_claude.py`'s
`ClaudeJsonlTailer` -> `rs.queue` (`StreamEvent("agent_message",
enriched)`) -> `TurnManager._drive_cli_run`'s `queue.get()` loop ->
`ws_callback` (== `save_ws_callback`) -> `TurnManager._publish_provider_
stream_event` -> `event_journal.publish_event`.

This test drives that FULL chain for real through the production
`TurnManager.run_turn` / `_drive_cli_run`, faking only the provider's own
subprocess I/O boundary (`Coordinator.provider_for_run`/`provider_for_
session`) — the one seam `provider_claude.py`'s real `ClaudeJsonlTailer`
sits behind. The fake's `start_run` puts a `StreamEvent` onto the SAME
`queue` object `_drive_cli_run` created and handed it, shaped EXACTLY
like `ClaudeJsonlTailer._decode_line`'s output for a real claude CLI
`type: "assistant"` jsonl line.

Companion tests isolate the layers below this one:
  - `scripts/test_claude_jsonl_tailer_real_line_dispatch.py`: does
    `jsonl_tailer.ClaudeJsonlTailer` dispatch a real appended line at all.
  - `scripts/test_provider_claude_tailer_to_queue_glue.py`: does
    `provider_claude.py`'s `_start_tailer_and_watchers`/`_dispatch_
    tailer_line` glue put that dispatched line onto `rs.queue`.

In its own file (own `_test_home.isolate(...)` -> own on-disk store path)
deliberately: `coordinator.lifecycle_commands` is a store-path-keyed
global singleton that `begin_turn` auto-binds to the current event loop
on first use and never releases within a test process — sharing a test
home with another `begin_turn`-using test (as this test originally did,
appended to `test_v2_projection_end_to_end.py`) makes ordering
deterministic-but-wrong: whichever test's `asyncio.run()` loop bound
first wins, the second raises `LifecycleAuthorityBound`/"not bound to
this loop". A dedicated file with a dedicated isolated home sidesteps it
by construction, matching how `test_provider_claude_tailer_to_queue_glue.py`
and `test_claude_jsonl_tailer_real_line_dispatch.py` are already scoped.

Run with:
    cd backend && .venv/bin/python scripts/test_native_provider_stream_content_journals.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-native-provider-stream-content-")


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


def test_real_dispatch_through_turn_manager_run_turn_journals_provider_stream_content() -> None:
    print("T1 real run_turn/_drive_cli_run dispatch journals provider-stream content")
    import orchestrator
    from backend.adapters.chat_adapter import ChatSurfaceAdapter
    from backend.surface_contract.frames import NodeUpsert
    from backend.surface_contract.identity import Focus, Ok, SurfaceCursor
    from backend.surface_contract.nodes import NodeKind
    from execution_template import prepare_execution
    from lifecycle_command_model import UserTurnIdentity
    from session_manager import manager as session_manager

    coordinator = orchestrator.Coordinator()
    tm = coordinator.turn_manager

    sess = session_manager.create(
        name="v2-provider-stream-content", model="haiku", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid) or sid
    assistant_text = "PONG"
    assistant_uuid = str(uuid.uuid4())

    # Live subscription set up BEFORE the turn runs — this is the
    # convergence half of the proof: not just that a cold `open_session`
    # replay eventually finds the content (which a slow/batched drain
    # could also satisfy), but that `ChatSurfaceAdapter`'s LIVE handler
    # (`_on_event_written` -> `_flush_event_written`) actually broadcasts
    # a `NodeUpsert` the instant the native pipeline's journal write
    # lands — the exact frame `/ws/v2/surface` streams to the browser
    # for a real turn.
    live_adapter = ChatSurfaceAdapter()
    live_adapter.bind()
    live_opened = live_adapter.open_session(root_id)
    check("live adapter opened the (empty) session", isinstance(live_opened, Ok))
    live_identity = live_opened.snapshot
    live_frames: list[object] = []
    live_cursor = SurfaceCursor(
        surface_id=root_id, incarnation=live_identity.incarnation,
        render_rev=live_identity.render_rev,
    )
    live_sub = live_adapter.subscribe((live_cursor,), Focus.OPENED, live_frames.append)

    class _FakeStreamingProvider:
        """Minimal fake at the SAME seam `_RetryProvider` (scripts/
        test_turn_gating.py) occupies: only `prepare_run`/`start_run`/
        `is_running_off_loop`/`cancel_turn`/`parse_rate_limit` — the
        methods `_drive_cli_run` actually calls on `provider`. Emits one
        real `agent_message` StreamEvent (the assistant's text response)
        BEFORE `complete`, exactly matching the order a real CLI turn's
        tailer dispatches: content lines while the process runs, then
        the `complete` event off `complete.json`/process exit."""

        KIND = "claude"

        def __init__(self) -> None:
            self.id = "claude"
            self.record = {
                "id": "claude", "kind": "claude",
                "generation": "00000000-0000-4000-8000-000000000001",
                "revision": 1, "execution_revision": 1, "runner": "native",
            }
            self._runs: dict = {}

        def prepare_run(self, **kw):
            return prepare_execution(
                self.record,
                runtime_policy={"native_sid_compatibility": {
                    "schema": 1, "engine": "claude-native", "node_id": "primary",
                    "thread_store_root": "/tmp/claude/projects",
                    "claude_project_namespace": "-tmp",
                }},
                **kw,
            )

        def start_run(self, *, execution, loop, queue):
            execution._try_commit_spawn()
            execution._mark_spawn_completed()
            run_id = execution.start_arguments()["run_id"]
            self._runs[run_id] = type(
                "RunState", (), {"popen": type("Popen", (), {"pid": os.getpid()})()},
            )()

            async def _emit() -> None:
                queue.put_nowait(type("E", (), {
                    "type": "agent_message",
                    "data": {
                        "type": "assistant", "uuid": assistant_uuid,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": assistant_text}],
                        },
                        "parent_tool_use_id": None,
                    },
                })())
                queue.put_nowait(type("E", (), {
                    "type": "complete",
                    "data": {
                        "success": True, "session_id": "fake-native-sid",
                        "token_usage": None,
                    },
                })())

            asyncio.run_coroutine_threadsafe(_emit(), loop)
            return True

        def is_running(self, run_id: str) -> bool:
            return False

        async def is_running_off_loop(self, run_id: str) -> bool:
            return False

        def cancel_turn(self, run_id: str) -> None:
            return None

        def parse_rate_limit(self, error, events):
            return None

    provider = _FakeStreamingProvider()
    # `_drive_cli_run` resolves the provider TWICE: once via
    # `provider_for_run` before the retry loop, then again via
    # `provider_for_session` inside `_refresh_provider_context` — called
    # on EVERY attempt, including the first — which is what
    # `prepare_run`/`start_run` actually end up using. Both must be
    # faked or the real `ClaudeProvider` wins the second resolution.
    coordinator.provider_for_run = lambda *_a, **_kw: provider
    coordinator.provider_for_session = lambda *_a, **_kw: provider

    ws_frames: list[dict] = []

    async def ws(frame: dict) -> None:
        ws_frames.append(frame)

    prompt_text = "Reply with exactly the single word: PONG."
    lifecycle_msg_id = f"lifecycle-{uuid.uuid4().hex}"

    async def _drive() -> None:
        # `_drive_cli_run`'s real path (unlike the stubbed path the
        # sibling prompt-journaling test uses) reaches `self.lifecycle.
        # register_execution_handle`, which asserts it's running on its
        # bound owning loop — bind/close around the drive exactly like
        # `scripts/test_turn_gating.py::_run_with_lifecycle`.
        #
        # The LIVE convergence half of this test needs the REAL
        # `event_journal_writer` to actually publish `EVENT_JOURNAL_
        # WRITTEN` on this loop (not the `_publish_written()` hand-fake
        # the sibling read-side-only tests use) so `chat_adapter`'s bus
        # subscription fires for real — matching what a running backend
        # does at startup (`main.py`'s own `bind_event_journal_loop` /
        # `loop_affinity.bind_main_loop` / `event_journal_writer.
        # register(bus)` calls).
        import loop_affinity
        from event_bus import bus
        from event_journal import bind_event_journal_loop, event_journal_writer
        loop = asyncio.get_running_loop()
        bind_event_journal_loop(loop)
        loop_affinity.bind_main_loop(loop)
        event_journal_writer.register(bus)

        await tm.lifecycle.bind()
        try:
            coordinator.user_prompt_manager.set_in_flight_lifecycle_msg_id(
                sid, lifecycle_msg_id,
            )
            await coordinator.lifecycle_commands.begin_turn(
                request_id=f"user-turn:{lifecycle_msg_id}:0:begin",
                session_id=sid,
                identity=UserTurnIdentity(
                    user_turn_id=lifecycle_msg_id, lifecycle_message_id=lifecycle_msg_id,
                ),
                execution_policy="single",
            )
            await tm.run_turn(
                session=session_manager.get_ref(sid),
                prompt=prompt_text,
                cli_prompt=prompt_text,
                app_session_id=sid,
                model="haiku",
                cwd="/tmp",
                ws_callback=ws,
                images=None,
                trace_step_name="native",
                session_id_field="agent_session_id",
                mode="native",
            )
        finally:
            await tm.lifecycle.close()

    try:
        asyncio.run(_drive())
    finally:
        live_sub.close()

    from event_journal import event_journal_reader
    rows, _total, _has_more = event_journal_reader.read_events(root_id, after_seq=0, limit=1000)
    content_rows = [
        r for r in rows
        if r.get("type") == "agent_message"
        and isinstance(r.get("data"), dict)
        and r["data"].get("type") == "assistant"
    ]
    check(
        "the REAL run_turn/_drive_cli_run dispatch journaled the "
        "provider's assistant-stream 'agent_message' row via "
        "_publish_provider_stream_event",
        bool(content_rows),
    )

    # (a) LIVE half of the convergence proof: the adapter subscribed
    # BEFORE the turn ran must have broadcast a NodeUpsert for the
    # assistant's text the instant the journal write landed.
    live_upserts = [
        f for f in live_frames
        if isinstance(f, NodeUpsert) and f.node.kind == NodeKind.ASSISTANT_TEXT
    ]
    check(
        "the live-subscribed adapter broadcast a NodeUpsert for the "
        "assistant text DURING the turn (not just discoverable on a "
        "later cold reload) — reproduces the field defect when absent: "
        "zero assistant node_upsert frames on a real turn",
        bool(live_upserts),
    )
    if live_upserts:
        check("the live-broadcast text matches the assistant response", live_upserts[0].node.payload.text == assistant_text)

    # (b) COLD-RELOAD half: a fresh adapter instance, full replay from
    # events.jsonl (no live-handler state carried over), must converge
    # on the SAME node — the REST snapshot path a page refresh takes.
    cold_adapter = ChatSurfaceAdapter()
    cold_opened = cold_adapter.open_session(root_id)
    check("cold reload open_session succeeded", isinstance(cold_opened, Ok))
    check("cold reload projected a non-empty turns tuple", cold_opened.value.turns != ())
    turn = cold_opened.value.turns[0]
    check("cold reload's turn prompt matches what was sent", turn.prompt is not None and turn.prompt.payload.text == prompt_text)
    cold_assistant_text_nodes = [
        n for n in cold_opened.value.live_turn_nodes if n.kind == NodeKind.ASSISTANT_TEXT
    ]
    check(
        "cold reload also projects an ASSISTANT_TEXT node from the "
        "journaled provider-stream row",
        bool(cold_assistant_text_nodes),
    )
    if cold_assistant_text_nodes:
        check("cold reload's projected text matches the assistant response", cold_assistant_text_nodes[0].payload.text == assistant_text)

    # (c) Convergence: live and cold-reload must agree on node identity
    # and payload, not just each independently finding "something".
    if live_upserts and cold_assistant_text_nodes:
        check(
            "live broadcast and cold reload converge on the SAME node_id",
            live_upserts[0].node.node_id == cold_assistant_text_nodes[0].node_id,
        )
        check(
            "live broadcast and cold reload converge on the SAME payload",
            live_upserts[0].node.payload == cold_assistant_text_nodes[0].payload,
        )


_TESTS = [
    test_real_dispatch_through_turn_manager_run_turn_journals_provider_stream_content,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
