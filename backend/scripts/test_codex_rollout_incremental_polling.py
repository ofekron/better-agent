from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import runner_codex


def _event(payload: dict) -> bytes:
    return (json.dumps({"type": "event_msg", "payload": payload}) + "\n").encode()


def _assert_matches_stateless(scanner, path: Path, start: int = 0) -> None:
    assert scanner.poll() == runner_codex._rollout_terminal_state(
        str(path), byte_offset=start,
    )


def test_unchanged_rollout_does_no_parse_work() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        path.write_bytes(b"".join(
            _event({"type": "agent_message", "message": f"working {index}"})
            for index in range(10_000)
        ))
        scanner = runner_codex._IncrementalRolloutScanner(path)
        _assert_matches_stateless(scanner, path)

        original_loads = runner_codex.json.loads
        parse_calls = 0

        def counted_loads(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return original_loads(*args, **kwargs)

        runner_codex.json.loads = counted_loads
        try:
            bytes_before = scanner.bytes_read
            for _ in range(20):
                scanner.poll()
            assert scanner.bytes_read == bytes_before
            assert parse_calls == 0
        finally:
            runner_codex.json.loads = original_loads


def test_incremental_results_match_stateless_across_boundaries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        prior = _event({
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 10}},
        })
        path.write_bytes(prior)
        start = len(prior)
        scanner = runner_codex._IncrementalRolloutScanner(path, byte_offset=start)
        _assert_matches_stateless(scanner, path, start)

        assistant = _event({"type": "agent_message", "message": "שלום"})
        terminal = _event({
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 14}},
        }) + _event({"type": "task_complete"})
        with path.open("ab") as file:
            file.write(b'{"malformed":\n')
            file.write(assistant[:-3])
        assert scanner.poll() == (None, {}, False)

        with path.open("ab") as file:
            file.write(assistant[-3:])
            file.write(terminal[:-1])
        assert scanner.poll()[0] is True
        _assert_matches_stateless(scanner, path, start)

        with path.open("ab") as file:
            file.write(b"\n")
        _assert_matches_stateless(scanner, path, start)


def test_replacement_resets_incremental_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        path.write_bytes(_event({"type": "agent_message", "message": "old"}))
        scanner = runner_codex._IncrementalRolloutScanner(path)
        assert scanner.poll() == (None, {}, True)

        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(_event({"type": "task_failed"}))
        os.replace(replacement, path)
        assert scanner.poll() == (False, {}, False)


if __name__ == "__main__":
    test_unchanged_rollout_does_no_parse_work()
    test_incremental_results_match_stateless_across_boundaries()
    test_replacement_resets_incremental_state()
    print("PASS: Codex live rollout polling is incremental and equivalent")
