"""Regression test for the live-reattach cursor-advance-before-apply bug.

Root cause: when a run's request_user_input (or any tool call) blocks
mid-flight and the backend restarts while the run is still alive, recovery
re-attaches by re-tailing the run's event source from the last PERSISTED
cursor. Each provider's tailer wiring (`_on_cursor` / `_on_tailer_progress`)
used to persist that cursor to `backend_state.json` the instant a line was
READ and enqueued — not once the consumer (`_drain_recovered_live_queue` /
turn_manager's live queue loop) actually applied it via `apply_event`. If the
backend restarted again before the queue drained, the in-memory queue (and
whatever sat in it) was destroyed, and the next reattach resumed tailing
PAST the already-advanced cursor — permanently skipping every
enqueued-but-unapplied event (including `request_user_input` tool_use/
tool_result pairs), which never reached the render tree even though the
underlying jsonl still had them.

Fix: each provider's `RunState` now tracks the eager tailer READ cursor
(`processed_line` / `processed_byte` / `processed_byte_offset`, used only for
in-memory drain-wait detection) separately from a durable APPLIED cursor
(`applied_line` / `applied_byte` / `applied_byte_offset`). `_write_backend_state`
persists the applied cursor, never the read cursor. `Provider.ack_applied_cursor`
is the only way the applied cursor advances, and it must be called by a
consumer only AFTER an event is actually applied to the render tree.

This test pins the contract directly for all four providers that share it
(openai/glm, gemini, codex, claude) — parity is required by project rules:
  1. `_write_backend_state` persists the APPLIED cursor, not a read cursor
     that has advanced ahead of it (the exact regression: before the fix,
     backend_state.json's cursor field mirrored the eager read cursor).
  2. `ack_applied_cursor` advances the persisted cursor once called.
  3. `ack_applied_cursor` is monotonic — an out-of-order/duplicate ack for a
     lower cursor value must never regress the durable cursor.

Run with:
    cd backend && .venv/bin/python scripts/test_cursor_ack_after_apply.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cursor-ack-")

from cursor_ledger_worker import worker as cursor_ledger_worker  # noqa: E402


class _FakePopen:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


def _mk_run_dir(run_id: str) -> Path:
    # active_run_catalog requires the run dir's own name to equal run_id and
    # its parent to be a plain (non-symlink) directory.
    runs_root = Path(tempfile.mkdtemp(prefix="bc-cursor-ack-runs-"))
    d = runs_root / run_id
    d.mkdir(parents=True)
    return d


def _read_backend_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "backend_state.json").read_text(encoding="utf-8"))


def _check_provider(
    *,
    provider_kind: str,
    make_provider,
    make_run_state,
    read_field: str,
    applied_field: str,
) -> bool:
    provider = make_provider()
    run_id = f"run-{provider_kind}"
    run_dir = _mk_run_dir(run_id)
    rs = make_run_state(run_dir, run_id)
    provider._runs[run_id] = rs

    # Simulate the tailer having READ far ahead (e.g. an entire long turn's
    # worth of tool_use/tool_result lines enqueued) while NONE of it has
    # been applied to the render tree yet.
    setattr(rs, read_field, 100)
    assert getattr(rs, applied_field) == 0, "applied cursor must start at 0"

    provider._write_backend_state(rs)
    persisted = _read_backend_state(run_dir)
    persisted_cursor_key = {
        "openai": "processed_line",
        "gemini": "processed_line",
        "codex": "processed_byte_offset",
        "claude": "processed_byte",
    }[provider_kind]
    if persisted[persisted_cursor_key] != 0:
        print(
            f"FAIL detail ({provider_kind}): backend_state.json persisted "
            f"{persisted_cursor_key}={persisted[persisted_cursor_key]} "
            "instead of the applied cursor (0) — the read-ahead cursor "
            "leaked into the durable resume point."
        )
        return False

    # Consumer applies through cursor 40 and acks.
    provider.ack_applied_cursor(run_id, 40)
    cursor_ledger_worker.flush_now(run_id)
    persisted = _read_backend_state(run_dir)
    if persisted[persisted_cursor_key] != 40:
        print(
            f"FAIL detail ({provider_kind}): expected applied cursor 40 "
            f"after ack, got {persisted[persisted_cursor_key]}"
        )
        return False

    # A lower/duplicate ack must never regress the durable cursor.
    provider.ack_applied_cursor(run_id, 10)
    cursor_ledger_worker.flush_now(run_id)
    persisted = _read_backend_state(run_dir)
    if persisted[persisted_cursor_key] != 40:
        print(
            f"FAIL detail ({provider_kind}): a lower ack (10) regressed "
            f"the durable cursor to {persisted[persisted_cursor_key]}"
        )
        return False

    # None must be a no-op (synthetic events never carry a tailer cursor).
    provider.ack_applied_cursor(run_id, None)
    cursor_ledger_worker.flush_now(run_id)
    persisted = _read_backend_state(run_dir)
    return persisted[persisted_cursor_key] == 40


def test_openai_ack_after_apply() -> bool:
    from provider_openai import OpenAIProvider, RunState

    def make_provider():
        return OpenAIProvider({"id": "test-openai", "kind": "openai"})

    def make_run_state(run_dir, run_id):
        import asyncio
        return RunState(
            run_id=run_id, run_dir=run_dir, popen=_FakePopen(999),
            mode="native", app_session_id="app-1", queue=asyncio.Queue(),
            started_at="2026-01-01T00:00:00",
        )

    return _check_provider(
        provider_kind="openai", make_provider=make_provider,
        make_run_state=make_run_state,
        read_field="processed_line", applied_field="applied_line",
    )


def test_gemini_ack_after_apply() -> bool:
    from provider_gemini import GeminiProvider, RunState

    def make_provider():
        return GeminiProvider({"id": "test-gemini", "kind": "gemini"})

    def make_run_state(run_dir, run_id):
        import asyncio
        return RunState(
            run_id=run_id, run_dir=run_dir, popen=_FakePopen(999),
            mode="native", app_session_id="app-1", queue=asyncio.Queue(),
            started_at="2026-01-01T00:00:00",
        )

    return _check_provider(
        provider_kind="gemini", make_provider=make_provider,
        make_run_state=make_run_state,
        read_field="processed_line", applied_field="applied_line",
    )


def test_codex_ack_after_apply() -> bool:
    from provider_codex import CodexProvider, RunState

    def make_provider():
        return CodexProvider({"id": "test-codex", "kind": "codex"})

    def make_run_state(run_dir, run_id):
        import asyncio
        return RunState(
            run_id=run_id, run_dir=run_dir, popen=_FakePopen(999),
            mode="native", app_session_id="app-1", queue=asyncio.Queue(),
            started_at="2026-01-01T00:00:00",
        )

    return _check_provider(
        provider_kind="codex", make_provider=make_provider,
        make_run_state=make_run_state,
        read_field="processed_byte_offset", applied_field="applied_byte_offset",
    )


def test_claude_ack_after_apply() -> bool:
    from provider_claude import ClaudeProvider, RunState

    def make_provider():
        return ClaudeProvider({"id": "test-claude", "kind": "claude"})

    def make_run_state(run_dir, run_id):
        import asyncio
        return RunState(
            run_id=run_id, run_dir=run_dir, popen=_FakePopen(999),
            mode="native", app_session_id="app-1", queue=asyncio.Queue(),
            started_at="2026-01-01T00:00:00",
        )

    return _check_provider(
        provider_kind="claude", make_provider=make_provider,
        make_run_state=make_run_state,
        read_field="processed_byte", applied_field="applied_byte",
    )


TESTS = [
    ("openai_ack_after_apply", test_openai_ack_after_apply),
    ("gemini_ack_after_apply", test_gemini_ack_after_apply),
    ("codex_ack_after_apply", test_codex_ack_after_apply),
    ("claude_ack_after_apply", test_claude_ack_after_apply),
]


def main() -> int:
    import shutil
    failures = []
    try:
        for name, fn in TESTS:
            ok = fn()
            print(("PASS" if ok else "FAIL") + f": {name}")
            if not ok:
                failures.append(name)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
