"""Regression tests for the promptable linger (lazy-persistent runner).

A new prompt on a native session whose runner is babysitter-lingering
used to CANCEL the linger (killing the background work the linger
exists to protect) and respawn a fresh --resume. The promptable linger
instead hands the turn to the lingering runner: the backend creates a
normal top-level run dir for the new run_id, drops a pointer into the
host's `handoff/` mailbox, and the runner serves the turn on its live
ClaudeSDKClient — no second CLI on the session, no ghost completion,
background work stays alive.

Locks (per the ADV-converged design):
  - gate: eligible single blocker → handoff, no cancel sentinel;
    ineligible (payload/env/record drift, stale heartbeat) → the old
    cancel+respawn path; third prompt during a handoff → defers on the
    handoff's TURN-end release.
  - runner: whitelist validation (unknown keys fail closed), exact
    user-line byte boundary (immune to prior-turn flush timing),
    slice-end stamping on the previous turn's state.json.
  - backend: handoff completion watcher releases at TURN end (not host
    process exit); host tailer hold/skip routing around the boundary;
    rejected / host-died handoffs fall back to respawn (prompt never
    lost).
  - recovery: `_replay_from_claude_jsonl` honors `jsonl_slice_end` so a
    restart mid-turn-N+1 can't re-attribute the newer turn's lines to
    turn N's message.

Run with:
    cd backend && .venv/bin/python scripts/test_linger_prompt_handoff.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-linger-handoff-")

import config_store  # noqa: E402
import runner  # noqa: E402
from provider_claude import ClaudeProvider, RunState  # noqa: E402
from runs_dir import runs_root  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []
_log = logging.getLogger("test-handoff")


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


class _FakePopen:
    def __init__(self, pid: int = 4242, alive: bool = True) -> None:
        self.pid = pid
        self.returncode = None if alive else 1

    def poll(self):
        return self.returncode


def _mk_provider() -> ClaudeProvider:
    rec = config_store.add_provider({
        "name": "Handoff Test", "kind": "claude", "mode": "subscription",
    })
    return ClaudeProvider(config_store.get_provider(rec["id"]) or rec)


def _start_run_kwargs(loop, queue, *, run_id, session_id, prompt="hello", model=None):
    return dict(
        run_id=run_id,
        prompt=prompt,
        cwd=".",
        loop=loop,
        queue=queue,
        model=model,
        reasoning_effort=None,
        session_id=session_id,
        mode="native",
        app_session_id="app-1",
        fork=False,
    )


def _mk_host(provider: ClaudeProvider, *, run_id="host-run", sid="sid-X") -> RunState:
    """A lingering host RunState that passes every eligibility check:
    alive popen, fresh heartbeat, spawn-time record/env snapshots, and an
    on-disk input.json identical to what the gate will build for the new
    prompt (only per-turn fields differ)."""
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload, _bare, _mode, _url = provider._build_input_payload(
        prompt="original prompt", images=None, files=None, cwd=".",
        model=None, reasoning_effort=None, session_id=sid, mode="native",
        app_session_id="app-1", source=None, disallowed_tools=None,
        setting_sources=None, backend_url=None, internal_token=None,
        fork=False, supervised=False, supervisor_agent_session_id=None,
        worker_agent_session_id=None, mssg_sender_session_id=None,
        is_worker=False, browser_harness_enabled=False,
        open_file_panel_enabled=False, continuation_chain=None,
        provider_run_config=None, capability_contexts=None,
        target_message_id=None, turn_run_id=None,
        disabled_builtin_extensions=None, provisioned_tool_profile="",
    )
    (run_dir / "input.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "runner_alive").write_text("{}", encoding="utf-8")
    host = RunState(
        run_id=run_id,
        run_dir=run_dir,
        popen=_FakePopen(),
        mode="native",
        app_session_id="app-1",
        queue=asyncio.Queue(),
        session_id=sid,
        lingering=True,
        turn_finalized=True,
        record_version_at_spawn=config_store.provider_record_version(provider.id),
        extra_env_at_spawn={},
    )
    provider._runs[run_id] = host
    return host


# ─── Test A — gate hands off to an eligible lingering runner ───────

async def test_a_gate_handoff(tmp: Path) -> None:
    provider = _mk_provider()
    host = _mk_host(provider, run_id="host-a", sid="sid-A")

    spawns: list[dict] = []
    provider._spawn_run = lambda **kw: spawns.append(kw)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    provider.start_run(**_start_run_kwargs(
        loop, q, run_id="new-a", session_id="sid-A", prompt="follow-up",
    ))

    new_dir = runs_root() / "new-a"
    pointers = sorted((host.run_dir / "handoff").glob("*-new-a.json"))
    _check("A1: no fresh spawn for an eligible handoff", len(spawns) == 0)
    _check("A2: linger NOT cancelled", not (host.run_dir / "cancel").exists())
    _check("A3: new run dir input.json written", (new_dir / "input.json").exists())
    _check("A4: time-prefixed pointer dropped in host mailbox", len(pointers) == 1)
    if pointers:
        _check(
            "A5: pointer targets the new run dir",
            json.loads(pointers[0].read_text())["run_dir"] == str(new_dir),
        )
    rs = provider._runs.get("new-a")
    _check(
        "A6: handoff RunState registered sharing host popen",
        rs is not None and rs.is_handoff_turn and rs.popen is host.popen,
    )
    _check("A7: host tracks the in-flight handoff", host.handoff_target is rs)
    # New turn's input payload only differs in per-turn fields.
    if (new_dir / "input.json").exists():
        new_payload = json.loads((new_dir / "input.json").read_text())
        host_payload = json.loads((host.run_dir / "input.json").read_text())
        diffs = {
            k for k in set(new_payload) | set(host_payload)
            if new_payload.get(k) != host_payload.get(k)
        }
        _check(
            "A8: payloads differ only in per-turn fields",
            diffs <= {"prompt", "images", "files", "target_message_id", "turn_run_id"},
            str(diffs),
        )
    # Cleanup pending bootstrap task noise.
    if rs is not None:
        provider._cleanup_run(rs.run_id)
        host.handoff_target = None


# ─── Test B — ineligibility falls back to cancel+respawn ──────────

async def test_b_gate_ineligible(tmp: Path) -> None:
    provider = _mk_provider()
    loop = asyncio.get_running_loop()

    # B1: payload drift (different model) → cancel+respawn.
    host = _mk_host(provider, run_id="host-b1", sid="sid-B1")
    spawns: list[dict] = []
    provider._spawn_run = lambda **kw: spawns.append(kw)
    provider.start_run(**_start_run_kwargs(
        loop, asyncio.Queue(), run_id="new-b1", session_id="sid-B1",
        model="claude-opus-4-8",
    ))
    _check("B1: model drift → linger cancelled (respawn path)",
           (host.run_dir / "cancel").exists())
    _check("B1b: no handoff registered", "new-b1" not in provider._runs)

    # B2: stale heartbeat → respawn path.
    host2 = _mk_host(provider, run_id="host-b2", sid="sid-B2")
    os.utime(host2.run_dir / "runner_alive", (1, 1))
    provider.start_run(**_start_run_kwargs(
        loop, asyncio.Queue(), run_id="new-b2", session_id="sid-B2",
    ))
    _check("B2: stale heartbeat → linger cancelled (respawn path)",
           (host2.run_dir / "cancel").exists())

    # B3: provider record drift → respawn path.
    host3 = _mk_host(provider, run_id="host-b3", sid="sid-B3")
    config_store.update_provider(provider.id, {"name": "Renamed"})
    provider.start_run(**_start_run_kwargs(
        loop, asyncio.Queue(), run_id="new-b3", session_id="sid-B3",
    ))
    _check("B3: provider record drift → linger cancelled (respawn path)",
           (host3.run_dir / "cancel").exists())

    # Release deferred gate waiters so the loop drains cleanly.
    for h in (host, host2, host3):
        provider._cleanup_run(h.run_id)
    await asyncio.sleep(0.05)


# ─── Test C — third prompt defers on the handoff's TURN-end release ─

async def test_c_third_prompt_defers(tmp: Path) -> None:
    provider = _mk_provider()
    host = _mk_host(provider, run_id="host-c", sid="sid-C")
    target = RunState(
        run_id="handoff-c", run_dir=runs_root() / "handoff-c",
        popen=host.popen, mode="native", app_session_id="app-1",
        queue=asyncio.Queue(), session_id="sid-C", is_handoff_turn=True,
        handoff_host=host,
    )
    provider._runs[target.run_id] = target
    host.handoff_target = target

    started: list[dict] = []
    real_start_run = provider.start_run
    provider._spawn_run = lambda **kw: started.append(kw)

    loop = asyncio.get_running_loop()
    real_start_run(**_start_run_kwargs(
        loop, asyncio.Queue(), run_id="third-c", session_id="sid-C",
    ))
    _check("C1: third prompt deferred while handoff in flight",
           len(started) == 0 and not (host.run_dir / "cancel").exists())

    # Turn-end release: cleanup of the handoff run fires released →
    # deferred prompt re-enters the gate. Host no longer lingers (so the
    # re-entry spawns fresh) — the point is the DEFER→RELEASE mechanics.
    host.lingering = False
    host.handoff_target = None
    provider._cleanup_run(target.run_id)
    for _ in range(50):
        if started:
            break
        await asyncio.sleep(0.01)
    _check("C2: deferred prompt resumes on handoff release", len(started) == 1)

    provider._cleanup_run(host.run_id)


# ─── Test D — host tailer routing around the boundary ─────────────

async def test_d_dispatch_routing(tmp: Path) -> None:
    provider = _mk_provider()
    host = _mk_host(provider, run_id="host-d", sid="sid-D")
    target = RunState(
        run_id="handoff-d", run_dir=runs_root() / "handoff-d",
        popen=host.popen, mode="native", app_session_id="app-1",
        queue=asyncio.Queue(), session_id="sid-D", is_handoff_turn=True,
    )
    host.handoff_target = target
    host.handoff_hold = []
    host.tailer = SimpleNamespace(processed_offset=0)

    orphaned: list[dict] = []
    provider._ingest_late_flush = lambda rs, e: orphaned.append(e)

    # Boundary unknown: lines are held, nothing orphaned.
    host.tailer.processed_offset = 100
    provider._dispatch_tailer_line(host, {"n": 1})
    host.tailer.processed_offset = 900
    provider._dispatch_tailer_line(host, {"n": 2})
    _check("D1: pre-arming lines held", len(host.handoff_hold) == 2 and not orphaned)

    # Arming at boundary 500: held line <500 → orphan; ≥500 → skipped.
    host.handoff_route_from = 500
    provider._flush_handoff_hold(host)
    _check("D2: held pre-boundary line orphan-ingested",
           [e["n"] for e in orphaned] == [1])

    # Live dispatch after arming follows the same split.
    host.tailer.processed_offset = 300
    provider._dispatch_tailer_line(host, {"n": 3})
    host.tailer.processed_offset = 700
    provider._dispatch_tailer_line(host, {"n": 4})
    _check("D3: post-arming routing (pre→orphan, post→skip)",
           [e["n"] for e in orphaned] == [1, 3])

    # Handoff finished: funnel restored for everything.
    host.handoff_target = None
    host.tailer.processed_offset = 999
    provider._dispatch_tailer_line(host, {"n": 5})
    _check("D4: funnel restored after handoff ends",
           [e["n"] for e in orphaned] == [1, 3, 5])
    provider._cleanup_run(host.run_id)


# ─── Test E — turn-end release + rejection/death fallbacks ────────

async def test_e_completion_watcher(tmp: Path) -> None:
    provider = _mk_provider()
    host = _mk_host(provider, run_id="host-e", sid="sid-E")

    # E1: handoff complete.json + live host → release at TURN end.
    run_dir = runs_root() / "handoff-e1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": "sid-E", "error": None,
        "token_usage": {"input_tokens": 5, "output_tokens": 2},
    }), encoding="utf-8")
    q: asyncio.Queue = asyncio.Queue()
    rs = RunState(
        run_id="handoff-e1", run_dir=run_dir, popen=host.popen,
        mode="native", app_session_id="app-1", queue=q,
        session_id="sid-E", is_handoff_turn=True, handoff_host=host,
    )
    provider._runs[rs.run_id] = rs
    host.handoff_target = rs
    await provider._watch_complete(rs)
    _check("E1: complete emitted on the handoff queue",
           not q.empty() and q.get_nowait().data.get("success") is True)
    _check("E2: released fired at TURN end (host still alive)",
           rs.released.is_set() and rs.run_id not in provider._runs)
    _check("E3: host handoff_target cleared", host.handoff_target is None)

    # E4: runner rejected the handoff → fallback respawn, host barred.
    run_dir2 = runs_root() / "handoff-e2"
    run_dir2.mkdir(parents=True, exist_ok=True)
    (run_dir2 / "complete.json").write_text(json.dumps({
        "success": False, "error": "handoff_rejected: input field 'model' differs",
    }), encoding="utf-8")
    respawns: list[dict] = []
    provider.start_run = lambda **kw: respawns.append(kw)
    rs2 = RunState(
        run_id="handoff-e2", run_dir=run_dir2, popen=host.popen,
        mode="native", app_session_id="app-1", queue=asyncio.Queue(),
        session_id="sid-E", is_handoff_turn=True, handoff_host=host,
        handoff_spawn_kwargs={"run_id": "handoff-e2", "queue": asyncio.Queue()},
    )
    provider._runs[rs2.run_id] = rs2
    host.handoff_target = rs2
    await provider._bootstrap_run(rs2)
    _check("E4: rejected handoff falls back to respawn", len(respawns) == 1)
    _check("E5: host barred from further handoffs",
           host.run_id in provider._handoff_barred)

    # E6: host died before pickup → fallback respawn.
    run_dir3 = runs_root() / "handoff-e3"
    run_dir3.mkdir(parents=True, exist_ok=True)
    rs3 = RunState(
        run_id="handoff-e3", run_dir=run_dir3, popen=_FakePopen(alive=False),
        mode="native", app_session_id="app-1", queue=asyncio.Queue(),
        session_id="sid-E", is_handoff_turn=True, handoff_host=host,
        handoff_spawn_kwargs={"run_id": "handoff-e3", "queue": asyncio.Queue()},
    )
    provider._runs[rs3.run_id] = rs3
    await provider._bootstrap_run(rs3)
    _check("E6: host death pre-pickup falls back to respawn", len(respawns) == 2)
    provider._cleanup_run(host.run_id)
    _check("E7: bar released when the host run is cleaned up",
           host.run_id not in provider._handoff_barred)


# ─── Test J — pointer pickup follows submission order ─────────────

def test_j_pointer_order(tmp: Path) -> None:
    run_dir = tmp / "host-j"
    box = run_dir / "handoff"
    box.mkdir(parents=True, exist_ok=True)
    # Reverse-alphabetical run_ids with in-order time prefixes: pickup
    # must follow the TIME prefix (submission order), not the run_id.
    (box / "00000000000000000001-zzz.json").write_text("{}", encoding="utf-8")
    (box / "00000000000000000002-aaa.json").write_text("{}", encoding="utf-8")
    first = runner._pending_handoff(run_dir)
    _check("J1: oldest pointer picked first",
           first is not None and first.name.endswith("-zzz.json"))


# ─── Test F — runner-side whitelist validation ────────────────────

def test_f_runner_validation() -> None:
    base = {"prompt": "a", "model": "m1", "cwd": ".", "permission": {"mode": "x"}}
    ok = dict(base, prompt="b", target_message_id="t2", turn_run_id="r2")
    _check("F1: per-turn fields may differ",
           runner._validate_handoff_input(base, ok) is None)
    _check("F2: model drift rejected",
           runner._validate_handoff_input(base, dict(base, model="m2")) is not None)
    _check("F3: unknown future key rejected (fail closed)",
           runner._validate_handoff_input(base, dict(base, new_flag=True)) is not None)
    _check("F4: missing key rejected",
           runner._validate_handoff_input(base, {"prompt": "b"}) is not None)


# ─── Test G — exact user-line byte boundary ───────────────────────

async def test_g_turn_boundary(tmp: Path) -> None:
    jsonl = tmp / "session.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    def _user_line(text: str) -> str:
        return json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }) + "\n"

    prior = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    jsonl.write_text(prior, encoding="utf-8")
    scan_from = jsonl.stat().st_size

    # Late continuation flush AFTER the settle-time EOF snapshot: a
    # task-notification user line + an assistant tail, THEN the new
    # turn's own user line — program order in the CLI.
    late_notification = _user_line("<task-notification>bg shell done</task-notification>")
    late_tail = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "noted"}]}}) + "\n"
    new_user = _user_line("the follow-up prompt")
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(late_notification)
        f.write(late_tail)
        f.write(new_user)

    expected = scan_from + len(late_notification.encode()) + len(late_tail.encode())
    boundary = await runner._resolve_turn_boundary(
        jsonl, scan_from, "the follow-up prompt", _log, timeout_s=2.0,
    )
    _check("G1: boundary = the new turn's own user line",
           boundary == expected, f"{boundary} != {expected}")

    missing = await runner._resolve_turn_boundary(
        jsonl, scan_from, "text that never appears", _log,
        timeout_s=0.3, poll_interval_s=0.05,
    )
    _check("G2: unmatched prompt times out to None", missing is None)


# ─── Test H — end-to-end handoff serve on a fake client ───────────

class _ServeFakeClient:
    """Fake ClaudeSDKClient whose query() appends the user line to the
    fake session jsonl — the same program order as the real CLI."""

    def __init__(self, jsonl: Path, messages) -> None:
        self._jsonl = jsonl
        self._messages = list(messages)

    async def query(self, prompt) -> None:
        text = prompt if isinstance(prompt, str) else ""
        if not isinstance(prompt, str):
            async for m in prompt:
                for block in (m.get("message", {}).get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text") or ""
        with self._jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }) + "\n")

    async def interrupt(self) -> None:
        return None

    def receive_response(self):
        msgs = self._messages

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


def _mk_result_message(*, usage, result):
    from claude_agent_sdk import ResultMessage  # type: ignore
    msg = ResultMessage.__new__(ResultMessage)
    msg.__dict__.update(dict(
        subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
        num_turns=1, session_id="sid-H", total_cost_usd=0.0, usage=usage,
        result=result, model_usage=None, stop_reason=None,
    ))
    return msg


def _mk_assistant_message(text: str, usage):
    from claude_agent_sdk import AssistantMessage  # type: ignore
    msg = AssistantMessage.__new__(AssistantMessage)
    msg.__dict__.update(dict(
        content=[{"type": "text", "text": text}], model="test-model",
        usage=usage, error=None, stop_reason=None, parent_tool_use_id=None,
    ))
    return msg


async def test_h_serve_handoff(tmp: Path) -> None:
    blocker_dir = tmp / "blocker"
    new_dir = tmp / "new-run"
    for d in (blocker_dir, new_dir):
        d.mkdir(parents=True, exist_ok=True)
    jsonl = tmp / "sess.jsonl"
    prior = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    jsonl.write_text(prior, encoding="utf-8")

    blocker_inputs = {"prompt": "orig", "model": "m", "cwd": str(tmp)}
    blocker_state_path = blocker_dir / "state.json"
    blocker_state_path.write_text(json.dumps({
        "session_id": "sid-H", "jsonl_path": str(jsonl),
        "pre_query_byte_offset": 0,
    }), encoding="utf-8")

    (new_dir / "input.json").write_text(json.dumps({
        "prompt": "the follow-up", "model": "m", "cwd": str(tmp),
        "images": [], "files": [],
    }), encoding="utf-8")
    pointer = blocker_dir / "handoff" / f"{new_dir.name}.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"run_dir": str(new_dir)}), encoding="utf-8")

    client = _ServeFakeClient(jsonl, [
        _mk_assistant_message("served!", {"input_tokens": 7, "output_tokens": 3}),
        _mk_result_message(usage={"input_tokens": 7, "output_tokens": 3}, result="served!"),
    ])
    served = await runner._serve_handoff_turn(
        pointer_path=pointer,
        blocker_inputs=blocker_inputs,
        client=client,
        session_state={"session_id": "sid-H", "jsonl_path": str(jsonl)},
        prev_turn_state_path=blocker_state_path,
        cwd=str(tmp),
        claude_config_dir=tmp / "cfg",
        interactive_permissions=False,
        current_turn_holder=[None],
        log=_log,
    )
    _check("H1: turn served", served is not None)
    complete = json.loads((new_dir / "complete.json").read_text())
    _check("H2: new run-level complete.json success",
           complete.get("success") is True and complete.get("sdk_output") == "served!")
    new_state = json.loads((new_dir / "state.json").read_text())
    boundary = new_state.get("pre_query_byte_offset")
    _check("H3: boundary = the served turn's user line",
           boundary == len(prior.encode()), f"{boundary}")
    blocker_state = json.loads(blocker_state_path.read_text())
    _check("H4: previous turn slice-end stamped at the boundary",
           blocker_state.get("jsonl_slice_end") == boundary)
    _check("H5: pointer consumed", not pointer.exists())
    _check("H6: turn-scoped runner_alive removed after complete",
           not (new_dir / "runner_alive").exists())

    # H7: mismatched payload → rejected, complete.json carries the marker.
    new_dir2 = tmp / "new-run2"
    new_dir2.mkdir(parents=True, exist_ok=True)
    (new_dir2 / "input.json").write_text(json.dumps({
        "prompt": "x", "model": "OTHER", "cwd": str(tmp),
    }), encoding="utf-8")
    pointer2 = blocker_dir / "handoff" / f"{new_dir2.name}.json"
    pointer2.write_text(json.dumps({"run_dir": str(new_dir2)}), encoding="utf-8")
    served2 = await runner._serve_handoff_turn(
        pointer_path=pointer2,
        blocker_inputs=blocker_inputs,
        client=client,
        session_state={"session_id": "sid-H", "jsonl_path": str(jsonl)},
        prev_turn_state_path=blocker_state_path,
        cwd=str(tmp),
        claude_config_dir=tmp / "cfg",
        interactive_permissions=False,
        current_turn_holder=[None],
        log=_log,
    )
    complete2 = json.loads((new_dir2 / "complete.json").read_text())
    _check("H7: mismatch rejected with handoff_rejected marker",
           served2 is None
           and str(complete2.get("error") or "").startswith("handoff_rejected")
           and not pointer2.exists())


# ─── Test I — recovery replay honors the slice end ────────────────

def test_i_replay_slice_bound(tmp: Path) -> None:
    import run_recovery
    jsonl = tmp / "sess.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    turn1 = json.dumps({
        "type": "assistant", "uuid": "u-turn1",
        "message": {"content": [{"type": "text", "text": "turn 1 reply"}]},
    }) + "\n"
    turn2_user = json.dumps({
        "type": "user", "uuid": "u-turn2u",
        "message": {"role": "user", "content": [{"type": "text", "text": "turn 2 prompt"}]},
    }) + "\n"
    turn2 = json.dumps({
        "type": "assistant", "uuid": "u-turn2",
        "message": {"content": [{"type": "text", "text": "turn 2 reply"}]},
    }) + "\n"
    jsonl.write_text(turn1 + turn2_user + turn2, encoding="utf-8")
    boundary = len(turn1.encode())

    run_dir = tmp / "turn1-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    inode = jsonl.stat().st_ino

    # Unbounded (legacy shape, no slice end): swallows turn 2 — the bug.
    (run_dir / "state.json").write_text(json.dumps({
        "jsonl_path": str(jsonl), "pre_query_byte_offset": 0,
        "pre_query_jsonl_inode": inode,
    }), encoding="utf-8")
    events = run_recovery._replay_from_claude_jsonl(run_dir)
    uuids = {e["data"].get("uuid") for e in events}
    _check("I1: unbounded replay includes the newer turn (the bug)",
           "u-turn2" in uuids)

    # Bounded by jsonl_slice_end: turn 1's replay stops at the boundary.
    (run_dir / "state.json").write_text(json.dumps({
        "jsonl_path": str(jsonl), "pre_query_byte_offset": 0,
        "pre_query_jsonl_inode": inode, "jsonl_slice_end": boundary,
    }), encoding="utf-8")
    events = run_recovery._replay_from_claude_jsonl(run_dir)
    uuids = {e["data"].get("uuid") for e in events}
    _check("I2: bounded replay excludes the newer turn's lines",
           "u-turn1" in uuids and "u-turn2" not in uuids and "u-turn2u" not in uuids,
           str(uuids))


async def _main() -> int:
    with tempfile.TemporaryDirectory(prefix="bc-linger-handoff-") as td:
        tmp = Path(td)
        print("Test A — gate hands off to an eligible lingering runner")
        await test_a_gate_handoff(tmp / "a")
        print("Test B — ineligibility falls back to cancel+respawn")
        await test_b_gate_ineligible(tmp / "b")
        print("Test C — third prompt defers on turn-end release")
        await test_c_third_prompt_defers(tmp / "c")
        print("Test D — host tailer routing around the boundary")
        await test_d_dispatch_routing(tmp / "d")
        print("Test E — turn-end release + rejection/death fallbacks")
        await test_e_completion_watcher(tmp / "e")
        print("Test F — runner-side whitelist validation")
        test_f_runner_validation()
        print("Test G — exact user-line byte boundary")
        await test_g_turn_boundary(tmp / "g")
        print("Test H — end-to-end handoff serve on a fake client")
        await test_h_serve_handoff(tmp / "h")
        print("Test I — recovery replay honors the slice end")
        test_i_replay_slice_bound(tmp / "i")
        print("Test J — pointer pickup follows submission order")
        test_j_pointer_order(tmp / "j")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
