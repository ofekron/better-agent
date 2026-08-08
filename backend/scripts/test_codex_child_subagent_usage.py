"""Unit coverage for `CodexProvider._record_child_terminal`'s real
per-child token-usage threading (backend/provider_codex.py).

Closes the gap where a Task-tool-dispatched Codex subagent's
`worker_complete` fact hardcoded `token_usage: None` — it now reads the
child's OWN rollout jsonl for a `turn.completed`/`turn.failed` event's
`usage`, the same extraction `_emit_complete_from_file` already uses for
the top-level run's own completion.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_codex_child_subagent_usage.py -q
    PYTHONPATH=. python3 backend/scripts/test_codex_child_subagent_usage.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-codex-child-usage-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider_codex import CodexProvider, RunState  # noqa: E402


def _make_turn_completed_event(usage: dict | None = None) -> dict:
    return {
        "type": "turn.completed",
        "usage": usage if usage is not None else {
            "input_tokens": 10, "output_tokens": 7, "cached_input_tokens": 3,
        },
    }


class _Popen:
    pid = os.getpid()

    def poll(self):
        return None


def _run_state(run_dir: Path) -> RunState:
    return RunState(
        run_id=run_dir.name, run_dir=run_dir, popen=_Popen(),
        mode="native", app_session_id="app", queue=asyncio.Queue(),
    )


def _write_child_rollout(run_dir: Path, *events: dict) -> str:
    path = run_dir / "child-rollout.jsonl"
    with path.open("wb") as f:
        for event in events:
            f.write(json.dumps(event).encode() + b"\n")
    return str(path)


def _child_source(jsonl_path: str, delegation_id: str) -> dict:
    return {
        "agent_id": "child", "source_key": delegation_id,
        "jsonl_path": jsonl_path, "start_byte": 0,
        "delegation_id": delegation_id,
    }


def test_record_child_terminal_threads_real_usage_from_turn_completed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        jsonl_path = _write_child_rollout(run_dir, _make_turn_completed_event())
        rs = _run_state(run_dir)
        source_key = "call_agent_child1"
        rs.child_sources[source_key] = _child_source(jsonl_path, "codex_subagent_child1")
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})

        asyncio.run(provider._record_child_terminal(rs, source_key, True))

        event = rs.queue.get_nowait()
        assert event.type == "worker_complete"
        assert event.data["delegation_id"] == "codex_subagent_child1"
        assert event.data["success"] is True
        assert event.data["token_usage"] == {
            "input_tokens": 10, "output_tokens": 7,
            "cache_read_input_tokens": 3, "total_tokens": 17,
        }


def test_record_child_terminal_usage_none_when_terminal_event_never_written() -> None:
    # Forced-False termination on parent shutdown — the child never
    # actually reached its own turn.completed/turn.failed line. Honest
    # None, never guessed.
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        jsonl_path = _write_child_rollout(
            run_dir,
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "partial"}},
        )
        rs = _run_state(run_dir)
        source_key = "call_agent_child2"
        rs.child_sources[source_key] = _child_source(jsonl_path, "codex_subagent_child2")
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})

        asyncio.run(provider._record_child_terminal(rs, source_key, False))

        event = rs.queue.get_nowait()
        assert event.data["success"] is False
        assert event.data["token_usage"] is None


def test_record_child_terminal_idempotent_only_emits_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        jsonl_path = _write_child_rollout(run_dir, _make_turn_completed_event())
        rs = _run_state(run_dir)
        source_key = "call_agent_child3"
        rs.child_sources[source_key] = _child_source(jsonl_path, "codex_subagent_child3")
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})

        asyncio.run(provider._record_child_terminal(rs, source_key, True))
        asyncio.run(provider._record_child_terminal(rs, source_key, True))

        assert rs.queue.qsize() == 1


_TESTS = [
    test_record_child_terminal_threads_real_usage_from_turn_completed,
    test_record_child_terminal_usage_none_when_terminal_event_never_written,
    test_record_child_terminal_idempotent_only_emits_once,
]


if __name__ == "__main__":
    failures = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
