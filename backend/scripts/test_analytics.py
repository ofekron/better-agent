"""Tests for usage analytics aggregation (backend/analytics.py).

Covers the pure ``aggregate`` (real-session filter, count breakdowns by
provider/model, turn filtering, bucket granularity, duration stats) and
the live ``compute_analytics`` wiring. Run standalone:

    cd backend && .venv/bin/python scripts/test_analytics.py

Like the other scripts/ tests, set BETTER_CLAUDE_HOME at import time so
the shared singletons don't touch the developer's real state; run each
scripts/ test standalone for reliable isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_test_home.isolate("bc-test-analytics-")

import analytics  # noqa: E402
import config_store  # noqa: E402
import llm_call_log  # noqa: E402
import session_store  # noqa: E402
import trace_collector  # noqa: E402

analytics.native_session_miner.iter_all_native_candidates = lambda: []

END = datetime(2026, 6, 1, 12, 0, 0)


def test_aggregate_excludes_internal_and_empty_sessions():
    start = END - timedelta(days=7)
    sessions = [
        {"id": "real", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team",
         "message_count": 4},
        # internal working-mode session -> excluded
        {"id": "internal", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team",
         "message_count": 4, "working_mode": "requirement_analysis_worker"},
        # empty stub -> excluded
        {"id": "empty", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team",
         "message_count": 0},
    ]
    out = analytics.aggregate(sessions, [], [], {"p1": {"id": "p1", "name": "Claude", "kind": "claude"}}, start, END)
    assert out["sessions"]["total"] == 1
    assert out["sessions"]["messages_total"] == 4


def test_aggregate_breaks_sessions_down_by_provider_and_model():
    start = END - timedelta(days=7)
    sessions = [
        {"id": "a", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team", "message_count": 2},
        {"id": "b", "created_at": (END - timedelta(days=2)).isoformat(),
         "provider_id": "p2", "model": "m2", "orchestration_mode": "native", "message_count": 2},
        {"id": "c", "created_at": (END - timedelta(days=30)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team", "message_count": 2},  # out of range
    ]
    pmap = {"p1": {"id": "p1", "name": "Claude", "kind": "claude"},
            "p2": {"id": "p2", "name": "Gemini", "kind": "gemini"}}
    out = analytics.aggregate(sessions, [], [], pmap, start, END)
    assert out["sessions"]["total"] == 2
    assert {p["name"]: p["count"] for p in out["sessions"]["by_provider"]} == {"Claude": 1, "Gemini": 1}
    assert {m["model"]: m["count"] for m in out["sessions"]["by_model"]} == {"m1": 1, "m2": 1}
    assert {o["mode"]: o["count"] for o in out["sessions"]["by_orchestration"]} == {"team": 1, "native": 1}


def test_aggregate_keeps_two_same_kind_providers_distinct():
    start = END - timedelta(days=7)
    sessions = [
        {"id": "a", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team", "message_count": 2},
        {"id": "b", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p2", "model": "m1", "orchestration_mode": "team", "message_count": 2},
    ]
    pmap = {"p1": {"id": "p1", "name": "Claude Pro", "kind": "claude"},
            "p2": {"id": "p2", "name": "Claude Personal", "kind": "claude"}}
    out = analytics.aggregate(sessions, [], [], pmap, start, END)
    assert len(out["sessions"]["by_provider"]) == 2
    assert {p["name"] for p in out["sessions"]["by_provider"]} == {"Claude Pro", "Claude Personal"}


def test_aggregate_turns_only_counted_for_real_sessions_in_range():
    start = END - timedelta(days=2)
    sessions = [
        {"id": "real", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team", "message_count": 4},
        {"id": "internal", "created_at": (END - timedelta(days=1)).isoformat(),
         "provider_id": "p1", "model": "m1", "orchestration_mode": "team",
         "message_count": 4, "working_mode": "search_worker"},
    ]
    traces = [
        {"session_id": "real", "timestamp": (END - timedelta(hours=2)).isoformat(), "duration_ms": 1000.0},
        {"session_id": "real", "timestamp": (END - timedelta(hours=1)).isoformat(), "duration_ms": 3000.0},
        {"session_id": "internal", "timestamp": END.isoformat(), "duration_ms": 500.0},  # excluded
        {"session_id": "orphan", "timestamp": END.isoformat(), "duration_ms": 500.0},     # excluded
        {"session_id": "real", "timestamp": (END - timedelta(days=10)).isoformat(), "duration_ms": 100.0},  # out of range
    ]
    pmap = {"p1": {"id": "p1", "name": "Claude", "kind": "claude"}}
    out = analytics.aggregate(sessions, traces, [], pmap, start, END)
    assert out["turns"]["total"] == 2
    # avg of [1000,3000]=2000; median of sorted [1000,3000]=2000
    assert out["turns"]["duration_avg_ms"] == 2000.0
    assert out["turns"]["duration_p50_ms"] == 2000.0
    prov = {p["name"]: p["turns"] for p in out["turns"]["by_provider"]}
    assert prov == {"Claude": 2}
    assert {m["model"]: m["turns"] for m in out["turns"]["by_model"]} == {"m1": 2}


def test_aggregate_uses_native_conversations_as_primary_usage_source():
    start = END - timedelta(days=2)
    native = [
        {
            "id": "native:/tmp/codex.jsonl",
            "sid": "codex-native-sid",
            "provider_kind": "codex",
            "provider_key": "native:codex",
            "provider_name": "Codex",
            "model": "unknown",
            "created_at": (END - timedelta(hours=6)).isoformat(),
            "message_count": 3,
            "orchestration_mode": "native",
            "turns": [
                {"timestamp": (END - timedelta(hours=6)).isoformat()},
                {"timestamp": (END - timedelta(hours=4)).isoformat()},
            ],
        }
    ]
    out = analytics.aggregate([], [], [], {}, start, END, native)
    assert out["sessions"]["total"] == 1
    assert out["sessions"]["messages_total"] == 3
    assert out["turns"]["total"] == 2
    assert {p["name"]: p["turns"] for p in out["turns"]["by_provider"]} == {"Codex": 2}


def test_aggregate_supplements_native_with_unindexed_ba_sessions_only():
    start = END - timedelta(days=2)
    sessions = [
        {"id": "native-sid", "created_at": (END - timedelta(hours=8)).isoformat(),
         "provider_id": "p1", "model": "sonnet", "orchestration_mode": "team", "message_count": 6},
        {"id": "ba-only", "created_at": (END - timedelta(hours=5)).isoformat(),
         "provider_id": "p1", "model": "sonnet", "orchestration_mode": "team", "message_count": 2},
    ]
    traces = [
        {"session_id": "native-sid", "timestamp": (END - timedelta(hours=7)).isoformat(), "duration_ms": 500.0},
        {"session_id": "ba-only", "timestamp": (END - timedelta(hours=4)).isoformat(), "duration_ms": 1000.0},
    ]
    native = [
        {
            "id": "native:/tmp/claude.jsonl",
            "sid": "native-sid",
            "provider_kind": "claude",
            "provider_key": "native:claude",
            "provider_name": "Claude",
            "model": "unknown",
            "created_at": (END - timedelta(hours=8)).isoformat(),
            "message_count": 4,
            "orchestration_mode": "native",
            "turns": [{"timestamp": (END - timedelta(hours=7)).isoformat()}],
        }
    ]
    pmap = {"p1": {"id": "p1", "name": "Claude", "kind": "claude"}}
    out = analytics.aggregate(sessions, traces, [], pmap, start, END, native)
    assert out["sessions"]["total"] == 2
    assert out["sessions"]["messages_total"] == 6
    assert out["turns"]["total"] == 2
    assert out["turns"]["duration_avg_ms"] == 1000.0


def test_aggregate_bucket_granularity_scales_with_span():
    assert analytics.aggregate([], [], [], {}, END - timedelta(days=2), END)["range"]["granularity"] == "hour"
    assert analytics.aggregate([], [], [], {}, END - timedelta(days=40), END)["range"]["granularity"] == "day"
    assert analytics.aggregate([], [], [], {}, END - timedelta(days=100), END)["range"]["granularity"] == "week"
    assert analytics.aggregate([], [], [], {}, END - timedelta(days=400), END)["range"]["granularity"] == "month"


def test_aggregate_accepts_explicit_granularity():
    start = END - timedelta(days=400)
    sessions = [{"id": "a", "created_at": (END - timedelta(days=8)).isoformat(),
                 "provider_id": "p1", "model": "m1", "orchestration_mode": "team", "message_count": 2}]
    out = analytics.aggregate(sessions, [], [], {}, start, END, granularity="day")
    assert out["range"]["granularity"] == "day"
    assert out["sessions"]["series"] == [{"t": (END - timedelta(days=8)).strftime("%Y-%m-%d"), "count": 1}]


def test_resolve_granularity_rejects_invalid_values():
    start = END - timedelta(days=400)
    assert analytics.resolve_granularity("week", start, END) == "week"
    assert analytics.resolve_granularity("bad", start, END) == "month"


def test_aggregate_empty_range_is_well_formed():
    out = analytics.aggregate([], [], [], {}, datetime(2026, 1, 1), datetime(2026, 1, 8))
    assert out["sessions"]["total"] == 0
    assert out["turns"]["total"] == 0
    assert out["sessions"]["series"] == []
    assert out["turns"]["series"] == []
    assert out["turns"]["duration_avg_ms"] == 0.0


def test_resolve_bounds_date_only_end_expands_to_end_of_day():
    s, e = analytics.resolve_bounds("2026-05-01", "2026-05-10")
    assert s == datetime(2026, 5, 1, 0, 0, 0)
    assert e == datetime(2026, 5, 10, 23, 59, 59, 999999)


def test_resolve_bounds_defaults_to_all_time_lower_bound():
    s, e = analytics.resolve_bounds(None, None)
    assert s == analytics.ANALYTICS_ALL_START
    assert e >= s


def test_resolve_bounds_invalid_start_still_falls_back_to_last_30_days():
    s, e = analytics.resolve_bounds("not-a-date", "2026-06-01")
    assert e == datetime(2026, 6, 1, 23, 59, 59, 999999)
    assert (e - s).days == 30


def test_default_bounds_include_native_session_older_than_30_days():
    start, end = analytics.resolve_bounds(None, "2026-06-01")
    native = [{
        "id": "native:/tmp/old.jsonl",
        "sid": "old-native-sid",
        "provider_kind": "claude",
        "provider_key": "native:claude",
        "provider_name": "Claude",
        "model": "unknown",
        "created_at": "2025-10-20T07:05:55.926000Z",
        "message_count": 2,
        "orchestration_mode": "native",
        "turns": [{"timestamp": "2025-10-20T07:05:55.926000Z"}],
    }]
    out = analytics.aggregate([], [], [], {}, start, end, native)
    assert out["sessions"]["total"] == 1
    assert out["turns"]["total"] == 1


def test_compute_analytics_reads_live_stores():
    p1 = config_store.add_provider({"name": "Claude", "kind": "claude", "mode": "subscription"})
    p2 = config_store.add_provider({
        "name": "Gemini",
        "kind": "gemini",
        "mode": "api_key",
        "api_key": "test-key",
    })
    s1 = session_store.create_session(name="a", model="glm-5.1", provider_id=p1["id"], orchestration_mode="team")
    s2 = session_store.create_session(name="b", model="gemini-2.5", provider_id=p2["id"], orchestration_mode="native")
    # give them a message so they pass the real-session filter
    session_store.write_session_full({**s1, "messages": [{"role": "user", "content": "hi", "timestamp": datetime.now().isoformat()}]})
    session_store.write_session_full({**s2, "messages": [{"role": "user", "content": "hi", "timestamp": datetime.now().isoformat()}]})

    idx = trace_collector._traces_dir() / "index.jsonl"
    idx.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()
    with open(idx, "a", encoding="utf-8") as f:
        for sid in (s1["id"], s2["id"]):
            f.write(json.dumps({"trace_id": "tr_" + sid[-8:], "session_id": sid,
                                "timestamp": now, "duration_ms": 1234.0, "step_count": 1,
                                "total_token_usage": {}, "user_prompt_preview": "x"}) + "\n")

    out = analytics.compute_analytics(*analytics.resolve_bounds(None, None))
    assert out["sessions"]["total"] == 2
    assert {"claude", "gemini"} <= {p["kind"] for p in out["providers"]}
    prov = {p["name"]: p["turns"] for p in out["turns"]["by_provider"]}
    assert prov["Claude"] == 1 and prov["Gemini"] == 1


def test_native_conversations_from_index_groups_sessions_and_turns():
    calls = []

    def fake_sql(sql, params=(), **kwargs):
        calls.append((sql, params, kwargs))
        if "SELECT path, ts_utc" in sql:
            return {
                "columns": ["path", "ts_utc"],
                "rows": [["/native/codex.jsonl", "2026-06-01T09:00:00.000000Z"]],
            }
        return {
            "columns": ["path", "sid", "cwd", "tag", "created_at", "message_count", "file_state_path"],
            "rows": [["/native/codex.jsonl", "sid-native", "/repo", "codex",
                      "2026-06-01T08:00:00.000000Z", 3, "/native/codex.jsonl"]],
        }

    original = analytics.native_transcript_index.run_readonly_sql
    original_state = analytics.native_transcript_index.quick_state
    analytics.native_transcript_index.run_readonly_sql = fake_sql
    analytics.native_transcript_index.quick_state = lambda: {
        "schema_ok": True,
        "covered": True,
        "usable": True,
    }
    try:
        out = analytics._native_conversations_from_index(
            datetime(2026, 6, 1, 0, 0, 0),
            datetime(2026, 6, 2, 0, 0, 0),
        )
    finally:
        analytics.native_transcript_index.run_readonly_sql = original
        analytics.native_transcript_index.quick_state = original_state

    assert len(calls) == 2
    assert calls[0][2]["timeout_s"] == analytics.NATIVE_ANALYTICS_SQL_TIMEOUT_SECONDS
    assert calls[1][2]["timeout_s"] == analytics.NATIVE_ANALYTICS_SQL_TIMEOUT_SECONDS
    assert out == [{
        "id": "native:/native/codex.jsonl",
        "sid": "sid-native",
        "cwd": "/repo",
        "provider_kind": "codex",
        "provider_key": "native:codex",
        "model": "unknown",
        "orchestration_mode": "native",
        "created_at": "2026-06-01T08:00:00.000000Z",
        "message_count": 3,
        "turns": [{"timestamp": "2026-06-01T09:00:00.000000Z"}],
    }]


def test_native_conversations_index_metadata_uses_file_state_not_meta_maxima():
    calls = []

    def fake_sql(sql, params=(), **kwargs):
        calls.append(sql)
        if "SELECT path, ts_utc" in sql:
            return {"columns": ["path", "ts_utc"], "rows": [["/native/codex.jsonl", "2026-06-01T09:00:00.000000Z"]]}
        return {"columns": ["path", "sid", "cwd", "tag", "created_at", "message_count", "file_state_path"], "rows": []}

    original = analytics.native_transcript_index.run_readonly_sql
    original_state = analytics.native_transcript_index.quick_state
    analytics.native_transcript_index.run_readonly_sql = fake_sql
    analytics.native_transcript_index.quick_state = lambda: {
        "schema_ok": True,
        "covered": True,
        "usable": True,
    }
    try:
        analytics._native_conversations_from_index(
            datetime(2026, 6, 1, 0, 0, 0),
            datetime(2026, 6, 2, 0, 0, 0),
        )
    finally:
        analytics.native_transcript_index.run_readonly_sql = original
        analytics.native_transcript_index.quick_state = original_state

    metadata_sql = calls[1].lower()
    assert "native_file_state" in metadata_sql
    assert "max(sid" not in metadata_sql
    assert "max(cwd" not in metadata_sql
    assert "max(tag" not in metadata_sql


def test_native_conversations_index_recovers_missing_file_state_metadata():
    calls = []

    def fake_sql(sql, params=(), **kwargs):
        calls.append((sql, params, kwargs))
        if "SELECT path, ts_utc" in sql:
            return {
                "columns": ["path", "ts_utc"],
                "rows": [["/native/orphan.jsonl", "2026-06-01T09:00:00.000000Z"]],
            }
        if "MAX(sid)" in sql:
            return {
                "columns": ["path", "sid", "cwd", "tag"],
                "rows": [["/native/orphan.jsonl", "sid-orphan", "/repo", "claude"]],
            }
        return {
            "columns": ["path", "sid", "cwd", "tag", "created_at", "message_count", "file_state_path"],
            "rows": [["/native/orphan.jsonl", "", "", "unknown",
                      "2026-06-01T08:00:00.000000Z", 2, None]],
        }

    original = analytics.native_transcript_index.run_readonly_sql
    original_state = analytics.native_transcript_index.quick_state
    analytics.native_transcript_index.run_readonly_sql = fake_sql
    analytics.native_transcript_index.quick_state = lambda: {
        "schema_ok": True,
        "covered": True,
        "usable": True,
    }
    try:
        out = analytics._native_conversations_from_index(
            datetime(2026, 6, 1, 0, 0, 0),
            datetime(2026, 6, 2, 0, 0, 0),
        )
    finally:
        analytics.native_transcript_index.run_readonly_sql = original
        analytics.native_transcript_index.quick_state = original_state

    assert len(calls) == 3
    assert calls[2][1][2:] == ("/native/orphan.jsonl",)
    assert out[0]["sid"] == "sid-orphan"
    assert out[0]["cwd"] == "/repo"
    assert out[0]["provider_kind"] == "claude"
    assert out[0]["provider_key"] == "native:claude"


def test_native_conversations_reads_raw_when_index_uncovered():
    class Candidate:
        key = "codex-old"
        sid = "codex-old"
        cwd = "/repo"
        format = "codex"
        transcript = "/native/old-codex.jsonl"

        def parse_elements(self):
            return [
                SimpleNamespace(kind="user_prompt", timestamp="2025-10-20T07:05:55.926Z"),
                SimpleNamespace(kind="assistant_text", timestamp="2025-10-20T07:05:56.000Z"),
            ]

    original_state = analytics.native_transcript_index.quick_state
    original_iter = analytics.native_session_miner.iter_all_native_candidates
    analytics.native_transcript_index.quick_state = lambda: {
        "schema_ok": False,
        "covered": False,
        "usable": False,
    }
    analytics.native_session_miner.iter_all_native_candidates = lambda: [Candidate()]
    try:
        out = analytics._native_conversations_from_index(
            datetime(2000, 1, 1),
            datetime(2026, 7, 6),
        )
    finally:
        analytics.native_transcript_index.quick_state = original_state
        analytics.native_session_miner.iter_all_native_candidates = original_iter

    assert out == [{
        "id": "native:/native/old-codex.jsonl",
        "sid": "codex-old",
        "cwd": "/repo",
        "provider_kind": "codex",
        "provider_key": "native:codex",
        "model": "unknown",
        "orchestration_mode": "native",
        "created_at": "2025-10-20T07:05:55.926000Z",
        "message_count": 2,
        "turns": [{"timestamp": "2025-10-20T07:05:55.926000Z"}],
    }]


def test_native_conversations_reads_raw_when_index_query_fails():
    class Candidate:
        key = "claude-old"
        sid = "claude-old"
        cwd = "/repo"
        format = "claude"
        transcript = "/native/old-claude.jsonl"

        def parse_elements(self):
            return [SimpleNamespace(kind="user_prompt", timestamp="2025-10-20T08:00:00Z")]

    original_state = analytics.native_transcript_index.quick_state
    original_sql = analytics.native_transcript_index.run_readonly_sql
    original_iter = analytics.native_session_miner.iter_all_native_candidates
    analytics.native_transcript_index.quick_state = lambda: {
        "schema_ok": True,
        "covered": True,
        "usable": True,
    }
    analytics.native_transcript_index.run_readonly_sql = lambda *_args, **_kwargs: {
        "error": "OperationalError: interrupted",
        "columns": [],
        "rows": [],
    }
    analytics.native_session_miner.iter_all_native_candidates = lambda: [Candidate()]
    try:
        out = analytics._native_conversations_from_index(
            datetime(2000, 1, 1),
            datetime(2026, 7, 6),
        )
    finally:
        analytics.native_transcript_index.quick_state = original_state
        analytics.native_transcript_index.run_readonly_sql = original_sql
        analytics.native_session_miner.iter_all_native_candidates = original_iter

    assert out[0]["id"] == "native:/native/old-claude.jsonl"
    assert out[0]["created_at"] == "2025-10-20T08:00:00Z"


def test_aggregate_llm_calls_from_single_log_shape():
    start = END - timedelta(days=7)
    calls = [
        {
            "id": "llm_a",
            "timestamp": (END - timedelta(hours=3)).isoformat(),
            "source": "turn",
            "reason": "manager",
            "provider_id": "p1",
            "provider_kind": "claude",
            "provider_name": "Claude",
            "model": "sonnet",
            "prompt_preview": "fix bug",
            "token_usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            "success": True,
        },
        {
            "id": "llm_b",
            "timestamp": (END - timedelta(hours=1)).isoformat(),
            "source": "rearranger",
            "reason": "session_tree_projection",
            "provider_id": "p2",
            "provider_kind": "gemini",
            "provider_name": "Gemini",
            "model": "gemini-2.5",
            "prompt_preview": "tree",
            "token_usage": {"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 5},
            "success": False,
            "error": "quota",
        },
        {
            "id": "old",
            "timestamp": (END - timedelta(days=30)).isoformat(),
            "source": "turn",
            "reason": "manager",
            "token_usage": {"total_tokens": 999},
        },
    ]
    pmap = {"p1": {"id": "p1", "name": "Claude", "kind": "claude"}}
    out = analytics.aggregate([], [], calls, pmap, start, END)
    llm = out["llm_calls"]
    assert llm["total"] == 2
    assert llm["token_usage"]["input_tokens"] == 12
    assert llm["token_usage"]["output_tokens"] == 7
    assert llm["token_usage"]["cache_read_input_tokens"] == 5
    assert llm["token_usage"]["total_tokens"] == 19
    assert {p["name"]: p["calls"] for p in llm["by_provider"]} == {"Claude": 1, "Gemini": 1}
    assert llm["recent"][0]["id"] == "llm_b"


def test_compute_analytics_includes_llm_log():
    llm_call_log.append_call(
        source="turn",
        reason="manager",
        provider_id="p1",
        provider_kind="claude",
        provider_name="Claude",
        model="sonnet",
        prompt="hello world",
        token_usage={"input_tokens": 1, "output_tokens": 2},
        success=True,
    )
    out = analytics.compute_analytics(*analytics.resolve_bounds(None, None))
    assert out["llm_calls"]["total"] >= 1
    assert out["llm_calls"]["recent"][0]["prompt_preview"] == "hello world"


_TESTS = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]


def main() -> int:
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
