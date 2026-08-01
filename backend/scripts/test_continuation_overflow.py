from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

# _provider_session_paths scans ba_home()/runs; isolate so it never touches the
# real home (the previous un-isolated build_continuation_prompt calls read it).
_HOME = _test_home.isolate("ba-continuation-")

from continuation import (  # noqa: E402
    CONTEXT_OVERFLOW_ERROR,
    _provider_session_paths,
    build_continuation_prompt,
    is_context_overflow_error,
    normalize_context_overflow_error,
)
from runs_dir import runs_root  # noqa: E402

import pytest  # noqa: E402


def _write_run_state(run_id: str, payload: dict, mtime: float | None = None) -> Path:
    """Create <runs>/<run_id>/backend_state.json with the given payload."""
    state_path = runs_root() / run_id / "backend_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(state_path, (mtime, mtime))
    return state_path


def test_overflow_normalization_covers_provider_phrases() -> None:
    cases = (
        "model_context_window_exceeded",
        "context_length_exceeded",
        "Context window exceeded",
        "maximum context length is 1,000 tokens",
        "input exceeds the token limit",
    )
    for case in cases:
        assert normalize_context_overflow_error(case) == CONTEXT_OVERFLOW_ERROR
    assert normalize_context_overflow_error("rate limit exceeded") is None


def test_continuation_prompt_renders_without_recall() -> None:
    prompt = build_continuation_prompt(
        prompt="keep going",
        app_session_id="abc123",
        continuation_chain=["oldsid1", "oldsid2"],
        reason="selector_changed",
    )
    assert "fresh subprocess" in prompt
    assert "abc123" in prompt
    assert "oldsid1" in prompt and "oldsid2" in prompt
    assert "fire_get_requirements" in prompt
    assert "get_requirements_results" in prompt
    assert "query_provider_native_transcript_index" not in prompt
    assert "native_element_fts" not in prompt
    assert "agent_session_id" in prompt
    assert "supervisor_agent_session_id" in prompt
    assert "already native ids" in prompt
    assert "keep going" in prompt
    # No recall machinery leaks into the rendered prompt.
    assert "recall_history" not in prompt
    assert "transcript excerpts" not in prompt


def test_continuation_prompt_renders_all_reasons() -> None:
    reasons = (
        "context_exceeded",
        "selector_changed",
        "agent_requested",
        "moved_project",
        "provider_capabilities_changed",
    )
    for reason in reasons:
        prompt = build_continuation_prompt(
            prompt="continue",
            app_session_id=f"session-{reason}",
            continuation_chain=["previous"],
            reason=reason,
        )
        assert "continue" in prompt
        assert f"session-{reason}" in prompt
        assert "previous" in prompt


# --- normalize edge branches ---

def test_normalize_none_and_empty_return_none() -> None:
    assert normalize_context_overflow_error(None) is None
    assert normalize_context_overflow_error("") is None


def test_normalize_secondary_context_branch() -> None:
    # "context" + a length/limit/exceed/window word but NO exact marker substring.
    for message in (
        "the context has been exceeded",  # context + exceed
        "context is over the length budget",  # context + length
    ):
        assert normalize_context_overflow_error(message) == CONTEXT_OVERFLOW_ERROR
    # "context" alone, or unrelated text, must NOT trip the secondary branch.
    assert normalize_context_overflow_error("just some context here") is None
    assert normalize_context_overflow_error("totally unrelated error") is None


def test_is_context_overflow_error_predicate() -> None:
    assert is_context_overflow_error("context window exceeded") is True
    assert is_context_overflow_error(None) is False
    assert is_context_overflow_error("rate limit exceeded") is False


# --- build_continuation_prompt chain-shape branches ---

def test_continuation_prompt_empty_chain_has_no_provider_blocks() -> None:
    prompt = build_continuation_prompt(
        prompt="go",
        app_session_id="s1",
        continuation_chain=[],
    )
    assert "Previous provider session ids" not in prompt
    assert "Previous provider session id paths" not in prompt
    assert "go" in prompt


def test_continuation_prompt_filters_blank_chain_entries() -> None:
    prompt = build_continuation_prompt(
        prompt="go",
        app_session_id="s1",
        continuation_chain=["  ", "", "real-sid"],
    )
    assert "real-sid" in prompt
    # Blank entries are dropped before the ids block is rendered.
    assert "  " not in prompt.split("Previous provider session ids:")[-1].splitlines()[0]


def test_continuation_prompt_renders_provider_paths_block() -> None:
    _write_run_state("run-a", {"session_id": "oldsid1", "jsonl_path": "/log/a.jsonl"})
    prompt = build_continuation_prompt(
        prompt="go",
        app_session_id="s1",
        continuation_chain=["oldsid1"],
    )
    assert "Previous provider session id paths" in prompt
    assert "/log/a.jsonl" in prompt


# --- _provider_session_paths run-dir scan ---

def test_provider_session_paths_empty_input() -> None:
    assert _provider_session_paths([]) == []


def test_provider_session_paths_no_runs_dir() -> None:
    # runs_root() exists (isolate may create it) but holds no matching run dirs.
    assert _provider_session_paths(["absent"]) == []


def test_provider_session_paths_matches_by_session_id() -> None:
    _write_run_state("run-a", {"session_id": "sid-a", "jsonl_path": "/log/a.jsonl"})
    _write_run_state(
        "run-b", {"session_id": "sid-b", "jsonl_path": "/log/b.jsonl"}
    )
    paths = dict(_provider_session_paths(["sid-a", "sid-b"]))
    assert paths == {"sid-a": "/log/a.jsonl", "sid-b": "/log/b.jsonl"}


def test_provider_session_paths_keeps_latest_mtime_for_duplicate_sid() -> None:
    _write_run_state(
        "run-old", {"session_id": "dup", "jsonl_path": "/log/old.jsonl"}, mtime=1000.0
    )
    _write_run_state(
        "run-new", {"session_id": "dup", "jsonl_path": "/log/new.jsonl"}, mtime=2000.0
    )
    paths = dict(_provider_session_paths(["dup"]))
    assert paths == {"dup": "/log/new.jsonl"}
    # Order follows the input wanted order, not mtime.
    assert _provider_session_paths(["dup", "absent"]) == [("dup", "/log/new.jsonl")]


def test_provider_session_paths_skips_missing_or_empty_jsonl_path() -> None:
    _write_run_state("run-nojsonl", {"session_id": "sid-x"})  # no jsonl_path
    _write_run_state("run-emptyjsonl", {"session_id": "sid-x", "jsonl_path": "  "})
    assert _provider_session_paths(["sid-x"]) == []


def test_provider_session_paths_skips_corrupt_state_file() -> None:
    state_path = runs_root() / "run-corrupt" / "backend_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")
    # A corrupt file is skipped, not fatal; a sibling good file still resolves.
    _write_run_state("run-good", {"session_id": "sid-y", "jsonl_path": "/log/y.jsonl"})
    paths = dict(_provider_session_paths(["sid-y"]))
    assert paths == {"sid-y": "/log/y.jsonl"}


def test_provider_session_paths_ignores_unwanted_session_ids() -> None:
    _write_run_state("run-other", {"session_id": "other", "jsonl_path": "/log/o.jsonl"})
    assert _provider_session_paths(["wanted"]) == []


def test_provider_session_paths_skips_older_duplicate_after_newer(monkeypatch) -> None:
    # Force a deterministic iteration order (newer mtime first) so the
    # keep-latest branch hits the "candidate older than stored" skip path.
    _write_run_state(
        "run-new", {"session_id": "dup", "jsonl_path": "/log/new.jsonl"}, mtime=2000.0
    )
    _write_run_state(
        "run-old", {"session_id": "dup", "jsonl_path": "/log/old.jsonl"}, mtime=1000.0
    )
    ordered = [runs_root() / "run-new", runs_root() / "run-old"]
    monkeypatch.setattr("continuation.iter_run_dirs", lambda *a, **k: iter(ordered))
    assert _provider_session_paths(["dup"]) == [("dup", "/log/new.jsonl")]


def test_provider_session_paths_tolerates_stat_oserror(monkeypatch) -> None:
    target = _write_run_state(
        "run-stat-fail", {"session_id": "sid-z", "jsonl_path": "/log/z.jsonl"}
    )
    target_str = str(target)
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path) == target_str:
            raise OSError("simulated stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    # read_text succeeds but the subsequent stat() raises -> run is skipped.
    assert _provider_session_paths(["sid-z"]) == []


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
