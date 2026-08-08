"""Tests for per-turn prompt compaction (no claude CLI subprocesses).

Pins the unit-level contracts of two compaction efforts:

  A. `<known_workers>` block uses positional columns under a single
     header line — `agent_session_id`, `mode`, `turns`, `description`
     all still appear; `last_active` is intentionally dropped.

  B. Supervisor verdict prompt has full + compact branches gated on
     `session["supervisor_bootstrap_received"]`. The flag is only
     flipped True AFTER a successful `_run_supervisor_turn` so a
     mid-turn failure on the first attempt leaves the flag False
     and the next attempt resends the full preamble. The
     verdict-response schema (DONE / AWAIT_USER / CONTINUE / FIX)
     appears verbatim in BOTH branches — it's the contract
     `_VERDICT_RE` parses.

Run with:
    cd backend && .venv/bin/python scripts/test_prompt_compaction.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-prompt-compaction-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402,F401  (import side-effects init the store)
from session_manager import manager as session_manager  # noqa: E402
from orchs.manager.bootstrap import format_known_workers  # noqa: E402
from orchs.supervisor import _verdict  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ── A. known_workers ────────────────────────────────────────────────

def test_format_known_workers_includes_required_fields() -> None:
    workers = [
        {
            "agent_session_id": "abc12345-aaaa-bbbb-cccc-dddddddddddd",
            "description": "Backend test suite migration",
            "orchestration_mode": "native",
            "last_active": "2024-01-01T00:00:00Z",
            "delegation_count": 3,
        },
        {
            "agent_session_id": "def67890-eeee-ffff-0000-111111111111",
            "description": "Frontend refactor worker",
            "orchestration_mode": "manager",
            "last_active": "2024-01-02T00:00:00Z",
            "delegation_count": 8,
        },
    ]
    out = format_known_workers(workers)
    # Header preserves the `agent_session_id` token so the manager
    # bootstrap prose ("use the agent_session_id as the opaque identifier")
    # still has its referent.
    assert "agent_session_id" in out, "missing 'agent_session_id' header token"
    for w in workers:
        assert w["agent_session_id"] in out, f"worker id {w['agent_session_id'][:8]} missing from output"
        assert w["description"] in out, f"description '{w['description']}' missing"
        assert w["orchestration_mode"] in out, f"mode '{w['orchestration_mode']}' missing"
        assert str(w["delegation_count"]) in out, f"turns '{w['delegation_count']}' missing"
    assert "<known_workers>" in out and "</known_workers>" in out, "envelope tags missing"


def test_format_known_workers_no_workers() -> None:
    out = format_known_workers([])
    assert "No workers yet" in out, "no-workers branch lost the 'No workers yet' sentinel"
    assert "<known_workers>" in out and "</known_workers>" in out, "envelope tags missing on empty branch"


# ── B. Verdict prompt full / compact ────────────────────────────────

_VERDICT_BULLETS = ("DONE", "AWAIT_USER", "CONTINUE", "FIX")


def test_verdict_prompt_full_contains_schema_and_framing() -> None:
    out = _verdict._build_verdict_prompt(
        primary_last_text="primary said X",
        original_user_request="user asked Y",
        primary_session_path="/tmp/agent.jsonl",
        primary_session_lines=42,
        compact=False,
    )
    for bullet in _VERDICT_BULLETS:
        assert bullet in out, f"full prompt missing verdict bullet '{bullet}'"
    # Framing rationale only present in full form — these are the
    # exact substrings the compact test asserts ABSENT, so they pin
    # the diff between branches.
    assert "lazy" in out, "full prompt missing 'lazy' framing"
    assert "worst cut" in out, "full prompt missing 'worst cut' rationale"


def test_verdict_prompt_compact_contains_schema_and_minimal_framing() -> None:
    out = _verdict._build_verdict_prompt(
        primary_last_text="primary said X",
        original_user_request="user asked Y",
        primary_session_path="/tmp/agent.jsonl",
        primary_session_lines=42,
        compact=True,
    )
    for bullet in _VERDICT_BULLETS:
        assert bullet in out, f"compact prompt missing verdict bullet '{bullet}'"
    # Role anchor still present — survives context compaction inside
    # the supervisor sub-session.
    assert "lazy" in out, "compact prompt lost role anchor ('lazy')"
    # Rationale paragraphs removed.
    assert "worst cut" not in out, "compact prompt still contains 'worst cut' rationale"
    assert "fabricated defaults" not in out, "compact prompt still contains 'fabricated defaults' rationale"


def test_choose_verdict_prompt_full_when_flag_false() -> None:
    session = {"supervisor_bootstrap_received": False}
    out = _verdict._choose_verdict_prompt(
        session,
        primary_last_text="x",
        original_user_request="y",
        primary_session_path=None,
        primary_session_lines=None,
    )
    assert "worst cut" in out, "flag=False did not select full preamble"


def test_choose_verdict_prompt_compact_when_flag_true() -> None:
    session = {"supervisor_bootstrap_received": True}
    out = _verdict._choose_verdict_prompt(
        session,
        primary_last_text="x",
        original_user_request="y",
        primary_session_path=None,
        primary_session_lines=None,
    )
    assert "worst cut" not in out, "flag=True did not select compact preamble"
    for bullet in _VERDICT_BULLETS:
        assert bullet in out, f"compact branch missing verdict bullet '{bullet}'"


# ── B. Gate behaviour (real request_verdict, mocked turn runner) ────


class _FakeCoordinator:
    """Minimal stand-in for Coordinator. `request_verdict` only
    calls `broadcast_session` on the error path; we let it be a no-op
    coroutine so we don't need a real WS broadcaster."""

    async def broadcast_session(self, *args, **kwargs):
        return None

    def is_session_cancelled(self, app_session_id: str) -> bool:
        return False


def _make_session_with_assistant_turn() -> dict:
    sess = session_manager.create(
        name="verdict-gate-test",
        model="sonnet",
        cwd="/tmp",
        orchestration_mode="native",
        source="cli",
    )
    # The verdict prompt needs a prior user message + a prior assistant
    # message to interpolate. `request_verdict` reads them via
    # `_last_user_request` / `_last_message_text`.
    sid = sess["id"]
    # Insert a user message + assistant message via session_manager so
    # they get persisted. We use the low-level _run path mirror.
    def _seed(s: dict) -> None:
        s["messages"] = (s.get("messages") or []) + [
            {"id": "u1", "role": "user", "content": "do thing"},
            {"id": "a1", "role": "assistant", "content": "did thing"},
        ]
    session_manager._run(sid, _seed, {"kind": "test_seed"})
    return session_manager.get(sid)


def test_first_verdict_failure_keeps_flag_false() -> None:
    sess = _make_session_with_assistant_turn()
    sid = sess["id"]

    assert sess.get("supervisor_bootstrap_received") is False, (
        f"flag did not default False: {sess.get('supervisor_bootstrap_received')!r}"
    )

    # First call: monkeypatch the turn runner to raise. flag must stay
    # False, return must be ("DONE", "") via the fail-open path.
    async def _raising(*args, **kwargs):
        raise RuntimeError("simulated supervisor failure")

    original_runner = _verdict._run_supervisor_turn
    _verdict._run_supervisor_turn = _raising  # type: ignore[assignment]

    async def _noop_ws(_payload):
        return None

    try:
        verdict, _instr = asyncio.run(
            _verdict.request_verdict(
                _FakeCoordinator(),  # type: ignore[arg-type]
                primary_session=sess,
                ws_callback=_noop_ws,
            )
        )
    finally:
        _verdict._run_supervisor_turn = original_runner  # type: ignore[assignment]

    assert verdict == "DONE", f"failed first call should fail-open to DONE, got {verdict!r}"
    fresh = session_manager.get(sid) or {}
    assert fresh.get("supervisor_bootstrap_received") is False, (
        f"flag flipped after a FAILED first call: {fresh.get('supervisor_bootstrap_received')!r}"
    )

    # Second call: monkeypatch the turn runner to succeed but write a
    # supervisor-source assistant message into the session so the
    # parser has something to read. Now the flag must flip True.
    async def _succeeding(coordinator, *, session, prompt, app_session_id, ws_callback, trace_step_name):
        def _append_supervisor_reply(s: dict) -> None:
            s["messages"] = (s.get("messages") or []) + [
                {"id": "s1", "role": "assistant", "source": "supervisor", "content": "DONE"},
            ]
        session_manager._run(app_session_id, _append_supervisor_reply, {"kind": "test_supervisor_reply"})

    _verdict._run_supervisor_turn = _succeeding  # type: ignore[assignment]
    try:
        verdict2, _ = asyncio.run(
            _verdict.request_verdict(
                _FakeCoordinator(),  # type: ignore[arg-type]
                primary_session=session_manager.get(sid),
                ws_callback=_noop_ws,
            )
        )
    finally:
        _verdict._run_supervisor_turn = original_runner  # type: ignore[assignment]

    assert verdict2 == "DONE", f"successful second call should parse DONE, got {verdict2!r}"
    fresh2 = session_manager.get(sid) or {}
    assert fresh2.get("supervisor_bootstrap_received") is True, (
        f"flag did NOT flip after a successful second call: {fresh2.get('supervisor_bootstrap_received')!r}"
    )


def test_mark_supervisor_bootstrap_received_is_idempotent() -> None:
    sess = session_manager.create(
        name="idem", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    assert sess.get("supervisor_bootstrap_received") is False, "default not False"
    session_manager.mark_supervisor_bootstrap_received(sid)
    after_one = session_manager.get(sid) or {}
    assert after_one.get("supervisor_bootstrap_received") is True, "first mark did not flip flag to True"
    session_manager.mark_supervisor_bootstrap_received(sid)
    after_two = session_manager.get(sid) or {}
    assert after_two.get("supervisor_bootstrap_received") is True, "second mark changed the value away from True"
    # Round-trip on disk so a reload would still see True. `mark_*`
    # persists via the debounced coordinator, so drain the durability
    # barrier before reading the on-disk record.
    session_manager.flush_root_persist(sid)
    fresh = session_store.get_session(sid)
    assert fresh is not None and fresh.get("supervisor_bootstrap_received") is True, (
        "on-disk round trip lost the True value"
    )


TESTS = [
    ("known_workers output preserves agent_session_id + description + mode + turns",
     test_format_known_workers_includes_required_fields),
    ("known_workers empty-list branch preserved",
     test_format_known_workers_no_workers),
    ("verdict prompt full form contains schema + framing",
     test_verdict_prompt_full_contains_schema_and_framing),
    ("verdict prompt compact form contains schema + minimal framing",
     test_verdict_prompt_compact_contains_schema_and_minimal_framing),
    ("_choose_verdict_prompt picks full when flag False",
     test_choose_verdict_prompt_full_when_flag_false),
    ("_choose_verdict_prompt picks compact when flag True",
     test_choose_verdict_prompt_compact_when_flag_true),
    ("first-verdict failure leaves flag False; subsequent success flips it True",
     test_first_verdict_failure_keeps_flag_false),
    ("mark_supervisor_bootstrap_received is idempotent + round-trips on disk",
     test_mark_supervisor_bootstrap_received_is_idempotent),
]


def main_run() -> None:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"{FAIL} {name}")
                if str(e):
                    print(f"  {e}")
            except Exception:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"{FAIL} {name}")
            else:
                print(f"{PASS} {name}")
        print()
        print(f"summary: {len(TESTS) - failed}/{len(TESTS)} passed")
        if failed:
            raise SystemExit(1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main_run()
