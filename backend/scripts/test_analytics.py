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

import _test_home
_test_home.isolate("bc-test-analytics-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import analytics  # noqa: E402
import config_store  # noqa: E402
import session_store  # noqa: E402
import trace_collector  # noqa: E402

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
    out = analytics.aggregate(sessions, [], {"p1": {"id": "p1", "name": "Claude", "kind": "claude"}}, start, END)
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
    out = analytics.aggregate(sessions, [], pmap, start, END)
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
    out = analytics.aggregate(sessions, [], pmap, start, END)
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
    out = analytics.aggregate(sessions, traces, pmap, start, END)
    assert out["turns"]["total"] == 2
    # avg of [1000,3000]=2000; median of sorted [1000,3000]=2000
    assert out["turns"]["duration_avg_ms"] == 2000.0
    assert out["turns"]["duration_p50_ms"] == 2000.0
    prov = {p["name"]: p["turns"] for p in out["turns"]["by_provider"]}
    assert prov == {"Claude": 2}
    assert {m["model"]: m["turns"] for m in out["turns"]["by_model"]} == {"m1": 2}


def test_aggregate_bucket_granularity_scales_with_span():
    assert analytics.aggregate([], [], {}, END - timedelta(days=2), END)["range"]["granularity"] == "hour"
    assert analytics.aggregate([], [], {}, END - timedelta(days=40), END)["range"]["granularity"] == "day"
    assert analytics.aggregate([], [], {}, END - timedelta(days=100), END)["range"]["granularity"] == "week"
    assert analytics.aggregate([], [], {}, END - timedelta(days=400), END)["range"]["granularity"] == "month"


def test_aggregate_empty_range_is_well_formed():
    out = analytics.aggregate([], [], {}, datetime(2026, 1, 1), datetime(2026, 1, 8))
    assert out["sessions"]["total"] == 0
    assert out["turns"]["total"] == 0
    assert out["sessions"]["series"] == []
    assert out["turns"]["series"] == []
    assert out["turns"]["duration_avg_ms"] == 0.0


def test_resolve_bounds_date_only_end_expands_to_end_of_day():
    s, e = analytics.resolve_bounds("2026-05-01", "2026-05-10")
    assert s == datetime(2026, 5, 1, 0, 0, 0)
    assert e == datetime(2026, 5, 10, 23, 59, 59, 999999)


def test_resolve_bounds_defaults_to_last_30_days():
    s, e = analytics.resolve_bounds(None, None)
    assert (e - s).days == 30


def test_compute_analytics_reads_live_stores():
    p1 = config_store.add_provider({"name": "Claude", "kind": "claude", "mode": "subscription"})
    p2 = config_store.add_provider({"name": "Gemini", "kind": "gemini", "mode": "subscription"})
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
