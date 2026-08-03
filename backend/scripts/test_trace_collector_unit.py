"""Unit coverage for trace_collector.py.

Per-turn trace collection: token-usage accounting helpers (normalize / merge /
aggregate / extract), turn-kind classification, the TraceStep/TraceCollector
model, and the JSONL index append+read under ``<ba_home>/traces/index.jsonl``
(file-locked append). Every branch is pinned with a real assertion against the
isolated test home (no production state touched).

Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_trace_collector_unit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import _test_home  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_HOME = _test_home.isolate("bc-test-trace-collector-unit-")

import trace_collector as tc  # noqa: E402
from paths import ba_home  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _traces_dir() -> Path:
    return ba_home() / "traces"


def _index_path() -> Path:
    return _traces_dir() / "index.jsonl"


def _clean_traces() -> None:
    """Tests share one isolated home; wipe the traces dir for fresh-slate tests."""
    shutil.rmtree(_traces_dir(), ignore_errors=True)


def _write_index(lines: list[str]) -> None:
    _traces_dir().mkdir(parents=True, exist_ok=True)
    _index_path().write_text("\n".join(lines) + "\n")


def _u(**kw) -> dict:
    """A minimal valid token_usage dict (base keys default 0)."""
    base = {"input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# classify_turn_kind
# --------------------------------------------------------------------------- #


def test_classify_team_sources():
    for src in ("mssg", "team_ask", "delegate_task"):
        assert tc.classify_turn_kind(
            source=src, user_initiated=False, user_prompt="") == src


def test_classify_user_initiated_direct():
    assert tc.classify_turn_kind(
        source=None, user_initiated=True, user_prompt="hi") == "direct_user"


def test_classify_no_source_but_prompt_is_direct():
    # user_initiated False, but empty source + a prompt still counts as direct
    assert tc.classify_turn_kind(
        source="", user_initiated=False, user_prompt="hi") == "direct_user"
    assert tc.classify_turn_kind(
        source="   ", user_initiated=False, user_prompt="hi") == "direct_user"


def test_classify_user_initiated_with_non_team_source_is_direct():
    assert tc.classify_turn_kind(
        source="scheduled", user_initiated=True, user_prompt="hi") == "direct_user"


def test_classify_non_team_source_not_user_initiated_is_system():
    assert tc.classify_turn_kind(
        source="scheduled", user_initiated=False, user_prompt="hi") == "system"


def test_classify_whitespace_prompt_falls_to_system():
    assert tc.classify_turn_kind(
        source="", user_initiated=True, user_prompt="   ") == "system"
    assert tc.classify_turn_kind(
        source=None, user_initiated=False, user_prompt="") == "system"


def test_classify_none_source():
    assert tc.classify_turn_kind(
        source=None, user_initiated=False, user_prompt="") == "system"


# --------------------------------------------------------------------------- #
# is_user_turn_index_entry
# --------------------------------------------------------------------------- #


def test_user_turn_kind_direct_user_true():
    assert tc.is_user_turn_index_entry({"turn_kind": "direct_user"}) is True


def test_user_turn_kind_other_false():
    assert tc.is_user_turn_index_entry({"turn_kind": "system"}) is False
    assert tc.is_user_turn_index_entry({"turn_kind": "mssg"}) is False


def test_user_turn_no_kind_source_present_false():
    assert tc.is_user_turn_index_entry({"source": "mssg"}) is False
    assert tc.is_user_turn_index_entry({"turn_source": "scheduled"}) is False


def test_user_turn_no_kind_no_source_preview_true():
    assert tc.is_user_turn_index_entry({"user_prompt_preview": "hello"}) is True


def test_user_turn_empty_entry_false():
    assert tc.is_user_turn_index_entry({}) is False
    assert tc.is_user_turn_index_entry(None) is False
    assert tc.is_user_turn_index_entry({"user_prompt_preview": ""}) is False


# --------------------------------------------------------------------------- #
# _normalize_token_usage
# --------------------------------------------------------------------------- #


def test_normalize_not_dict_returns_none():
    assert tc._normalize_token_usage(None) is None
    assert tc._normalize_token_usage([1]) is None
    assert tc._normalize_token_usage("x") is None


def test_normalize_no_token_keys_returns_none():
    assert tc._normalize_token_usage({"foo": 1}) is None


def test_normalize_base_keys_coerced():
    out = tc._normalize_token_usage({"input_tokens": "5", "output_tokens": None})
    assert out == _u(input_tokens=5, output_tokens=0)


def test_normalize_flat_cache_breakdown():
    out = tc._normalize_token_usage(
        {"input_tokens": 1, "cache_creation_5m_tokens": 7})
    assert out["cache_creation_5m_tokens"] == 7


def test_normalize_nested_cache_breakdown():
    out = tc._normalize_token_usage({
        "input_tokens": 1,
        "cache_creation": {"ephemeral_1h_input_tokens": 4},
    })
    assert out["cache_creation_1h_tokens"] == 4
    assert "cache_creation_5m_tokens" not in out


def test_normalize_flat_wins_over_nested():
    out = tc._normalize_token_usage({
        "input_tokens": 1,
        "cache_creation_5m_tokens": 9,
        "cache_creation": {"ephemeral_5m_input_tokens": 1},
    })
    assert out["cache_creation_5m_tokens"] == 9


def test_normalize_nested_not_dict_ignored():
    out = tc._normalize_token_usage(
        {"input_tokens": 1, "cache_creation": "nope"})
    assert "cache_creation_5m_tokens" not in out
    assert out["input_tokens"] == 1


# --------------------------------------------------------------------------- #
# _merge_usage / merge_token_usages
# --------------------------------------------------------------------------- #


def test_merge_all_none_returns_none():
    assert tc._merge_usage([None, {}, {"foo": 1}]) is None
    assert tc.merge_token_usages([None]) is None


def test_merge_single():
    assert tc._merge_usage([_u(input_tokens=3)]) == _u(input_tokens=3)


def test_merge_sums_base_keys():
    out = tc._merge_usage([_u(input_tokens=1, output_tokens=2),
                           _u(input_tokens=4, output_tokens=5)])
    assert out == _u(input_tokens=5, output_tokens=7)


def test_merge_breakdown_only_where_present():
    a = {**_u(input_tokens=1), "cache_creation_5m_tokens": 2}
    b = _u(input_tokens=1)  # no breakdown
    out = tc._merge_usage([a, b])
    assert out["input_tokens"] == 2
    assert out["cache_creation_5m_tokens"] == 2


def test_merge_breakdown_absent_in_all():
    out = tc._merge_usage([_u(input_tokens=1), _u(input_tokens=1)])
    assert "cache_creation_5m_tokens" not in out


# --------------------------------------------------------------------------- #
# aggregate_claude_usage_snapshots
# --------------------------------------------------------------------------- #


def test_aggregate_snapshots_keyed_last_wins():
    out = tc.aggregate_claude_usage_snapshots([
        ("m1", _u(input_tokens=1)),
        ("m1", _u(input_tokens=9)),  # duplicate id -> last wins
    ])
    assert out["input_tokens"] == 9


def test_aggregate_snapshots_unkeyed_appended():
    out = tc.aggregate_claude_usage_snapshots([
        (None, _u(input_tokens=1)),
        (None, _u(input_tokens=2)),
    ])
    assert out["input_tokens"] == 3


def test_aggregate_snapshots_mixed():
    out = tc.aggregate_claude_usage_snapshots([
        ("m1", _u(input_tokens=1)),
        (None, _u(input_tokens=2)),
        ("m1", _u(input_tokens=5)),
    ])
    assert out["input_tokens"] == 7  # 5 (keyed last) + 2 (unkeyed)


def test_aggregate_snapshots_all_none():
    assert tc.aggregate_claude_usage_snapshots([
        ("m1", None), (None, {"foo": 1})]) is None


# --------------------------------------------------------------------------- #
# aggregate_claude_turn_usage
# --------------------------------------------------------------------------- #


def test_turn_usage_result_present_wins():
    out = tc.aggregate_claude_turn_usage(
        [("m1", _u(input_tokens=1))], result_usage=_u(input_tokens=99))
    assert out["input_tokens"] == 99


def test_turn_usage_result_none_falls_back():
    out = tc.aggregate_claude_turn_usage(
        [("m1", _u(input_tokens=7))], result_usage=None)
    assert out["input_tokens"] == 7


def test_turn_usage_result_unnormalizable_falls_back():
    out = tc.aggregate_claude_turn_usage(
        [("m1", _u(input_tokens=7))], result_usage={"foo": 1})
    assert out["input_tokens"] == 7


# --------------------------------------------------------------------------- #
# extract_token_usage
# --------------------------------------------------------------------------- #


def test_extract_complete_envelope_wins():
    events = [
        {"type": "complete", "data": {"token_usage": _u(input_tokens=42)}},
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m1", "usage": _u(input_tokens=1)}}},
    ]
    assert tc.extract_token_usage(events)["input_tokens"] == 42


def test_extract_complete_envelope_raw_passthrough():
    # the complete envelope's token_usage is returned as-is (not normalized)
    events = [{"type": "complete", "data": {"token_usage": {"custom": 5}}} ]
    assert tc.extract_token_usage(events) == {"custom": 5}


def test_extract_complete_empty_token_usage_falls_back():
    events = [
        {"type": "complete", "data": {"token_usage": None}},
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m1", "usage": _u(input_tokens=3)}}},
    ]
    assert tc.extract_token_usage(events)["input_tokens"] == 3


def test_extract_fallback_assistant_usage():
    events = [
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m1", "usage": _u(input_tokens=2)}}},
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m2", "usage": _u(input_tokens=4)}}},
    ]
    assert tc.extract_token_usage(events)["input_tokens"] == 6


def test_extract_skips_non_dict_events():
    events = ["nope", None, {"type": "complete", "data": {"token_usage": _u(input_tokens=1)}}]
    assert tc.extract_token_usage(events)["input_tokens"] == 1


def test_extract_fallback_skips_non_dict_events():
    # no complete envelope -> fallback loop must skip non-dict entries.
    events = [
        "nope",
        None,
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m1", "usage": _u(input_tokens=5)}}},
    ]
    assert tc.extract_token_usage(events)["input_tokens"] == 5


def test_extract_skips_non_assistant_messages():
    events = [{"type": "agent_message", "data": {"type": "user",
        "message": {"id": "m1", "usage": _u(input_tokens=9)}}}]
    assert tc.extract_token_usage(events) is None


def test_extract_skips_non_dict_message_and_usage():
    events = [
        {"type": "agent_message", "data": {"type": "assistant", "message": "x"}},
        {"type": "agent_message", "data": {"type": "assistant",
            "message": {"id": "m1", "usage": "nope"}}},
    ]
    assert tc.extract_token_usage(events) is None


def test_extract_no_matches_returns_none():
    assert tc.extract_token_usage([]) is None
    assert tc.extract_token_usage([{"type": "other"}]) is None


# --------------------------------------------------------------------------- #
# extract_provider_result_token_usage
# --------------------------------------------------------------------------- #


def test_provider_result_token_usage_present():
    out = tc.extract_provider_result_token_usage({"token_usage": _u(input_tokens=8)})
    assert out["input_tokens"] == 8


def test_provider_result_falls_back_to_events():
    result = {"events": [
        {"type": "complete", "data": {"token_usage": _u(input_tokens=11)}}]}
    assert tc.extract_provider_result_token_usage(result)["input_tokens"] == 11


def test_provider_result_unnormalizable_falls_back():
    result = {"token_usage": {"foo": 1}, "events": [
        {"type": "complete", "data": {"token_usage": _u(input_tokens=2)}}]}
    assert tc.extract_provider_result_token_usage(result)["input_tokens"] == 2


# --------------------------------------------------------------------------- #
# TraceStep
# --------------------------------------------------------------------------- #


def test_step_duration_none_before_end():
    step = tc.TraceStep("tool", thread_id="t1")
    step.start()
    assert step.duration_ms is None


def test_step_duration_after_end():
    step = tc.TraceStep("tool")
    step.start()
    step.end()
    assert step.duration_ms is not None and step.duration_ms >= 0


def test_step_duration_none_when_never_started():
    step = tc.TraceStep("tool")
    assert step.duration_ms is None


def test_step_to_dict_shape():
    step = tc.TraceStep("tool", thread_id="t1", thread_name="n", ephemeral=True)
    step.input_prompt = "p"
    step.raw_output = "r"
    step.parsed_output = {"a": 1}
    step.parse_error = None
    step.token_usage = _u(input_tokens=1)
    step.error = "boom"
    step.subagent_types = ["x"]
    d = step.to_dict()
    assert d["step_type"] == "tool"
    assert d["thread_id"] == "t1"
    assert d["ephemeral"] is True
    assert d["parsed_output"] == {"a": 1}
    assert d["error"] == "boom"
    assert d["subagent_types"] == ["x"]
    assert d["duration_ms"] is None


# --------------------------------------------------------------------------- #
# TraceCollector — construction + synchronous props
# --------------------------------------------------------------------------- #


def test_collector_construction():
    c = tc.TraceCollector("s1", "hello world", source="  mssg  ", user_initiated=False)
    assert c.trace_id.startswith("tr_")
    assert c.session_id == "s1"
    assert c.turn_source == "mssg"  # stripped
    assert c.turn_kind == "mssg"
    assert c.user_initiated is False
    assert c.total_duration_ms is None  # not finalized
    assert c.total_token_usage == {}
    assert c.steps == []


def test_collector_total_duration_after_finalize():
    c = tc.TraceCollector("s1", "hi")
    c.finalize()
    assert c.total_duration_ms is not None


def test_collector_total_token_usage_sums_steps():
    c = tc.TraceCollector("s1", "hi")
    s1 = c.start_step("a"); s1.token_usage = _u(input_tokens=1)
    s2 = c.start_step("b"); s2.token_usage = {"input_tokens": 2, "extra": 5}
    asyncio.run(c.end_step(s1))
    asyncio.run(c.end_step(s2))
    total = c.total_token_usage
    assert total["input_tokens"] == 3
    assert total["extra"] == 5


def test_collector_total_token_usage_none_val_treated_zero():
    c = tc.TraceCollector("s1", "hi")
    s = c.start_step("a"); s.token_usage = {"input_tokens": None}
    asyncio.run(c.end_step(s))
    assert c.total_token_usage["input_tokens"] == 0


def test_to_index_entry_shape():
    c = tc.TraceCollector("s1", "x" * 200, user_initiated=True)
    s = c.start_step("a")
    asyncio.run(c.end_step(s))
    entry = c.to_index_entry()
    assert entry["trace_id"] == c.trace_id
    assert entry["session_id"] == "s1"
    assert entry["user_prompt_preview"] == "x" * 100  # truncated to 100
    assert entry["turn_kind"] == "direct_user"
    assert entry["user_initiated"] is True
    assert entry["step_count"] == 1
    assert "total_token_usage" in entry


# --------------------------------------------------------------------------- #
# TraceCollector.end_step — async WS streaming
# --------------------------------------------------------------------------- #


def test_end_step_appends_without_callback():
    c = tc.TraceCollector("s1", "hi")
    s = c.start_step("a")
    asyncio.run(c.end_step(s))
    assert len(c.steps) == 1


def test_end_step_streams_via_callback():
    received: list[dict] = []

    async def cb(payload: dict):
        received.append(payload)

    c = tc.TraceCollector("s1", "hi")
    c.set_ws_callback(cb)
    s = c.start_step("a")
    asyncio.run(c.end_step(s))
    assert len(received) == 1
    assert received[0]["type"] == "trace_step"
    assert received[0]["data"]["trace_id"] == c.trace_id
    assert received[0]["data"]["step_index"] == 0


def test_end_step_callback_exception_swallowed():
    async def bad_cb(payload: dict):
        raise RuntimeError("boom")

    c = tc.TraceCollector("s1", "hi")
    c.set_ws_callback(bad_cb)
    s = c.start_step("a")
    # must not raise; step still recorded
    asyncio.run(c.end_step(s))
    assert len(c.steps) == 1


# --------------------------------------------------------------------------- #
# save()
# --------------------------------------------------------------------------- #


def test_save_appends_index_line():
    c = tc.TraceCollector("s1", "first")
    asyncio.run(c.end_step(c.start_step("a")))
    c.save()
    c2 = tc.TraceCollector("s1", "second")
    c2.save()
    lines = _index_path().read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["user_prompt_preview"] == "first"
    assert json.loads(lines[1])["user_prompt_preview"] == "second"


def test_save_creates_traces_dir():
    _clean_traces()
    assert not _traces_dir().exists()
    tc.TraceCollector("s1", "hi").save()
    assert _index_path().exists()


def test_save_swallows_exception(monkeypatch):
    _clean_traces()
    assert not _index_path().exists()

    @contextmanager
    def _boom():
        raise OSError("disk full")
        yield

    monkeypatch.setattr(tc, "_index_lock", _boom)
    c = tc.TraceCollector("s1", "hi")
    c.save()  # must not raise
    assert not _index_path().exists()  # nothing written


# --------------------------------------------------------------------------- #
# iter_trace_index
# --------------------------------------------------------------------------- #


def test_iter_index_missing_file_yields_nothing():
    _clean_traces()
    assert list(tc.iter_trace_index()) == []


def test_iter_index_yields_entries():
    _write_index([
        json.dumps({"trace_id": "t1"}),
        json.dumps({"trace_id": "t2"}),
    ])
    entries = list(tc.iter_trace_index())
    assert [e["trace_id"] for e in entries] == ["t1", "t2"]


def test_iter_index_skips_blank_and_malformed():
    _write_index([
        json.dumps({"trace_id": "t1"}),
        "",
        "   ",
        "{not json}",
        json.dumps({"trace_id": "t2"}),
    ])
    entries = list(tc.iter_trace_index())
    assert [e["trace_id"] for e in entries] == ["t1", "t2"]
