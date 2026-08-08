#!/usr/bin/env python3
"""Failing-first coverage for Package E6 (ADR 0009 `UsageAnalytics`):
pure `{provider_id, model, period}` aggregation
(`backend/adapters/usage_analytics.py`) plus `RunsSurfaceAdapter.
usage_analytics()`'s wiring (cursor pagination, stale-cursor detection,
honest-absence state). Every test here failed before this pass:
`aggregate_usage_rows` did not exist, and `RunsSurfaceAdapter.
usage_analytics()` unconditionally returned `Rebuilding` (see the
superseded `test_usage_analytics_is_rebuilding` this pass replaces in
`test_surface_adapters.py`).

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched) — same idiom as test_adapter_runs.py /
test_store_access.py.

Run:
    PYTHONPATH=.:./backend:./sdk python3 -m pytest backend/scripts/test_usage_analytics.py -q
    PYTHONPATH=.:./backend:./sdk python3 backend/scripts/test_usage_analytics.py   # __main__ fallback
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-usage-analytics-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import llm_call_log  # noqa: E402  (bare — store_access._resolve aliases onto this instance)

from backend.adapters.runs_adapter import RunsSurfaceAdapter  # noqa: E402
from backend.adapters.store_access import LlmCallRecord, store_access  # noqa: E402
from backend.adapters.usage_analytics import aggregate_usage_rows  # noqa: E402
from backend.surface_contract.identity import Ok, StaleCursor  # noqa: E402
from backend.surface_contract.runs_surface import UsageMeasureState  # noqa: E402


def _call(
    *,
    provider_id: str | None = "prov-1",
    model: str | None = "model-x",
    timestamp: str = "2026-01-15T10:00:00Z",
    trace_id: str | None = None,
    token_usage: dict | None = None,
) -> LlmCallRecord:
    return LlmCallRecord(
        id=f"llm_{uuid.uuid4().hex[:12]}",
        timestamp=timestamp,
        provider_id=provider_id,
        model=model,
        trace_id=trace_id,
        token_usage=dict(token_usage or {}),
    )


# ---------------------------------------------------------------------------
# Pure aggregation — backend/adapters/usage_analytics.py
# ---------------------------------------------------------------------------

def test_aggregate_groups_same_provider_model_period_into_one_row() -> None:
    calls = (
        _call(trace_id="t1", token_usage={"input_tokens": 10, "output_tokens": 5}),
        _call(trace_id="t2", token_usage={"input_tokens": 3, "output_tokens": 2}),
    )
    rows = aggregate_usage_rows(calls)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider_id == "prov-1"
    assert row.model == "model-x"
    assert row.period == "2026-01-15"
    assert row.state == UsageMeasureState.REPORTED
    assert row.tokens == 20
    assert row.turns == 2
    assert row.cost is None


def test_aggregate_separates_rows_by_calendar_day() -> None:
    calls = (
        _call(timestamp="2026-01-15T23:59:00Z", trace_id="t1", token_usage={"total_tokens": 1}),
        _call(timestamp="2026-01-16T00:01:00Z", trace_id="t2", token_usage={"total_tokens": 1}),
    )
    rows = aggregate_usage_rows(calls)
    assert {r.period for r in rows} == {"2026-01-15", "2026-01-16"}
    assert len(rows) == 2


def test_aggregate_unreported_when_no_token_count_key_present() -> None:
    calls = (
        _call(trace_id="t1", token_usage={}),
        _call(trace_id="t2", token_usage={"duration_ms": 500}),  # no *_tokens key
    )
    rows = aggregate_usage_rows(calls)
    assert len(rows) == 1
    row = rows[0]
    assert row.state == UsageMeasureState.UNREPORTED
    assert row.tokens is None
    # turns is a real, independently-known count regardless of reporting.
    assert row.turns == 2


def test_aggregate_reported_zero_tokens_is_an_honest_zero_not_unreported() -> None:
    rows = aggregate_usage_rows((_call(trace_id="t1", token_usage={"total_tokens": 0}),))
    assert len(rows) == 1
    assert rows[0].state == UsageMeasureState.REPORTED
    assert rows[0].tokens == 0


def test_aggregate_partial_reporting_sums_only_calls_that_reported() -> None:
    calls = (
        _call(trace_id="t1", token_usage={"total_tokens": 40}),
        _call(trace_id="t2", token_usage={}),  # this call never reported usage
    )
    rows = aggregate_usage_rows(calls)
    assert len(rows) == 1
    row = rows[0]
    assert row.state == UsageMeasureState.REPORTED
    assert row.tokens == 40  # only the reporting call's tokens, never fabricated for t2
    assert row.turns == 2  # call volume is still fully counted


def test_aggregate_excludes_calls_missing_provider_id_or_model() -> None:
    calls = (
        _call(provider_id=None, trace_id="t1", token_usage={"total_tokens": 5}),
        _call(model=None, trace_id="t2", token_usage={"total_tokens": 5}),
        _call(trace_id="t3", token_usage={"total_tokens": 5}),
    )
    rows = aggregate_usage_rows(calls)
    assert len(rows) == 1
    assert rows[0].turns == 1


def test_aggregate_excludes_calls_with_unparseable_timestamp() -> None:
    rows = aggregate_usage_rows((_call(timestamp="not-a-date", trace_id="t1"),))
    assert rows == ()


def test_aggregate_turns_dedupes_by_trace_id_within_one_bucket() -> None:
    calls = (
        _call(trace_id="shared-trace"),
        _call(trace_id="shared-trace"),
        _call(trace_id="shared-trace"),
        _call(trace_id=None),  # no trace linkage — counts as its own turn
    )
    rows = aggregate_usage_rows(calls)
    assert len(rows) == 1
    assert rows[0].turns == 2  # 1 deduped trace + 1 standalone untraced call


def test_aggregate_sorts_period_desc_then_provider_then_model() -> None:
    calls = (
        _call(provider_id="prov-b", model="m1", timestamp="2026-01-10T00:00:00Z", trace_id="t1"),
        _call(provider_id="prov-a", model="m1", timestamp="2026-01-11T00:00:00Z", trace_id="t2"),
        _call(provider_id="prov-a", model="m2", timestamp="2026-01-11T00:00:00Z", trace_id="t3"),
    )
    rows = aggregate_usage_rows(calls)
    assert [(r.period, r.provider_id, r.model) for r in rows] == [
        ("2026-01-11", "prov-a", "m1"),
        ("2026-01-11", "prov-a", "m2"),
        ("2026-01-10", "prov-b", "m1"),
    ]


# ---------------------------------------------------------------------------
# Adapter wiring — backend/adapters/runs_adapter.py's usage_analytics()
# ---------------------------------------------------------------------------

def _seed_call(provider_id: str, model: str, *, tokens: int = 10) -> None:
    llm_call_log.append_call(
        source="turn", reason="test", provider_id=provider_id, model=model,
        trace_id=f"tr_{uuid.uuid4().hex[:12]}", token_usage={"total_tokens": tokens},
    )


def _all_rows(adapter: RunsSurfaceAdapter) -> list:
    """Walks every page — a fresh `provider_id` (random uuid) can land on
    any page once other rows share this process's test home, so a
    correctness check must never assume page 1 alone."""
    rows: list = []
    cursor = None
    for _ in range(50):
        result = adapter.usage_analytics(cursor)
        assert isinstance(result, Ok), result
        rows.extend(result.value.rows)
        cursor = result.value.next_cursor
        if cursor is None:
            break
    return rows


def test_runs_adapter_usage_analytics_empty_log_is_ok_not_rebuilding() -> None:
    """Honest-absence at the surface level: no calls at all is a real,
    empty `Ok` page — never the permanent `Rebuilding` stub this replaces."""
    provider_id = f"prov-{uuid.uuid4().hex}"
    adapter = RunsSurfaceAdapter()
    result = adapter.usage_analytics(None)
    assert isinstance(result, Ok), result
    assert all(row.provider_id != provider_id for row in result.value.rows)


def test_runs_adapter_usage_analytics_reflects_seeded_calls() -> None:
    provider_id = f"prov-{uuid.uuid4().hex}"
    model = "seeded-model"
    _seed_call(provider_id, model, tokens=7)
    _seed_call(provider_id, model, tokens=3)

    adapter = RunsSurfaceAdapter()
    match = next(r for r in _all_rows(adapter) if r.provider_id == provider_id)
    assert match.model == model
    assert match.state == UsageMeasureState.REPORTED
    assert match.tokens == 10
    assert match.turns == 2


def test_runs_adapter_usage_analytics_paginates_without_overlap_or_gap() -> None:
    provider_id = f"prov-{uuid.uuid4().hex}"
    for i in range(55):
        _seed_call(provider_id, f"model-{i}", tokens=1)

    adapter = RunsSurfaceAdapter()
    first = adapter.usage_analytics(None)
    assert isinstance(first, Ok), first
    ours_first = [r for r in first.value.rows if r.provider_id == provider_id]

    seen_models: set[str] = {r.model for r in ours_first}
    cursor = first.value.next_cursor
    pages = 1
    while cursor is not None and pages < 10:
        nxt = adapter.usage_analytics(cursor)
        assert isinstance(nxt, Ok), nxt
        seen_models |= {r.model for r in nxt.value.rows if r.provider_id == provider_id}
        cursor = nxt.value.next_cursor
        pages += 1
    assert seen_models == {f"model-{i}" for i in range(55)}


def test_runs_adapter_usage_analytics_stale_cursor_across_adapter_incarnations() -> None:
    provider_id = f"prov-{uuid.uuid4().hex}"
    for i in range(55):
        _seed_call(provider_id, f"stale-model-{i}", tokens=1)

    first_adapter = RunsSurfaceAdapter()
    first = first_adapter.usage_analytics(None)
    assert isinstance(first, Ok), first
    assert first.value.next_cursor is not None

    second_adapter = RunsSurfaceAdapter()
    stale = second_adapter.usage_analytics(first.value.next_cursor)
    assert isinstance(stale, StaleCursor), stale


_TESTS = [
    test_aggregate_groups_same_provider_model_period_into_one_row,
    test_aggregate_separates_rows_by_calendar_day,
    test_aggregate_unreported_when_no_token_count_key_present,
    test_aggregate_reported_zero_tokens_is_an_honest_zero_not_unreported,
    test_aggregate_partial_reporting_sums_only_calls_that_reported,
    test_aggregate_excludes_calls_missing_provider_id_or_model,
    test_aggregate_excludes_calls_with_unparseable_timestamp,
    test_aggregate_turns_dedupes_by_trace_id_within_one_bucket,
    test_aggregate_sorts_period_desc_then_provider_then_model,
    test_runs_adapter_usage_analytics_empty_log_is_ok_not_rebuilding,
    test_runs_adapter_usage_analytics_reflects_seeded_calls,
    test_runs_adapter_usage_analytics_paginates_without_overlap_or_gap,
    test_runs_adapter_usage_analytics_stale_cursor_across_adapter_incarnations,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"ok      {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL    {fn.__name__}")
            import traceback
            traceback.print_exc()
        except Exception:
            failures += 1
            print(f"ERROR   {fn.__name__}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
