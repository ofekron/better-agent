"""Pull-only `UsageAnalytics` aggregation (ADR 0009, package E6).

Source chosen: `backend.llm_call_log` (via `store_access.
list_llm_call_records()`) — the SAME durable, per-call, provider_id/model-
keyed token log `backend/analytics.py`'s legacy `GET /api/analytics`
already sums its own token/call totals from (see that module's docstring:
llm_calls.by_provider/by_model/token_usage). It is chosen over the two
other candidate sources surveyed for this package:

  - Session/turn-journal token usage (`trace_collector.
    extract_provider_result_token_usage`, the `session_token_usage_added`
    internal fact) — `analytics.py`'s own docstring documents this figure
    as cumulative-context-sized ("the known 'context window math' issue"),
    unsafe to sum across turns. `llm_call_log` entries are NOT this same
    number re-exposed: `turn_manager.py` calls `extract_provider_result_
    token_usage(primary_result)` once per individual provider call (each
    `trace.end_step`) and hands that single call's own usage to `llm_call_
    log.append_call` — additive across calls, not a running total.
  - The `ofek-dev.model-traffic` extension's proxy-observed thread facts
    (`backend/traffic_facts_api.py`) — not reachable from this worktree at
    all (extension code lives in a sibling `better-agent-private` checkout
    only), flag-gated OFF by default (`BA_SURFACE_TRAFFIC_SOURCE`), and
    republished as `persist=False` bus events with NO durable store behind
    them (`traffic_facts_api.py`'s own docstring: "not a consumer...
    nothing folds these facts into the render tree yet") — nothing here to
    cold-aggregate even when the flag is on.

Computed cold, on every read, no cache/index: `list_llm_call_records()`
streams the whole `llm_calls.jsonl` file once per call, the same O(file
size) cost `analytics.compute_analytics`'s own `list(llm_call_log.
iter_calls())` already pays — no sqlite/chat_index-style rebuildable
projection is warranted (measure first; this is not expensive enough to
justify the machinery).

Honest-absence (ADR 0009's descriptor-honesty invariant): a
`(provider_id, model, period)` row is `REPORTED` when at least one call in
that bucket carries a real token-count key (`input_tokens`/`output_tokens`/
`total_tokens`/either cache-tokens key — `llm_call_log._normalize_usage`
only ever writes one of these when the provider result actually reported
a countable figure); a bucket where every call's `token_usage` carried
none of those keys is `UNREPORTED`, never a fabricated `0`. `turns` is
always a real, independently-known count (call occurrence is observed
directly by this backend, not "reported" by the provider), so it is
populated in both states. `cost` has no computation source anywhere in
this codebase — always `None`.

Calls missing `provider_id` or `model` are excluded from aggregation
entirely: `UsageRow.provider_id` is a descriptor ref (ADR 0006 §6) and
"never invent" forbids substituting a synthetic `"unknown"` ref no
descriptor would resolve.

`turns` counts distinct `trace_id`s per bucket, not raw call rows: one
`TraceCollector` instance (`trace_id` assigned once) can call `end_step`
— and therefore `llm_call_log.append_call` — more than once for a single
turn (multi-step tool-use loops), so raw row count would overcount turns.
This mirrors `analytics.py`'s own "turns" section, which counts trace
index entries (one per trace/turn), not individual LLM calls. A call
carrying no `trace_id` at all contributes its own standalone turn (no
other identity to dedup it against).
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.adapters.store_access import LlmCallRecord
from backend.surface_contract.runs_surface import UsageMeasureState, UsageRow

_PERIOD_FORMAT = "%Y-%m-%d"  # one row per calendar day (UTC) per (provider_id, model)

_TOKEN_COUNT_KEYS = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _period_for(timestamp: str) -> str | None:
    dt = _parse_ts(timestamp)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime(_PERIOD_FORMAT)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _call_tokens(usage: dict) -> int | None:
    """`None` when the call carries no token-count key at all (honest
    absence); otherwise the real total, mirroring `analytics.py`'s own
    `total_tokens or (input+output)` fallback formula."""
    if not usage or not any(key in usage for key in _TOKEN_COUNT_KEYS):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total:
        return int(total)
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


class _Bucket:
    __slots__ = ("trace_ids", "untraced", "reported", "tokens")

    def __init__(self) -> None:
        self.trace_ids: set[str] = set()
        self.untraced = 0
        self.reported = False
        self.tokens = 0


def aggregate_usage_rows(calls: tuple[LlmCallRecord, ...]) -> tuple[UsageRow, ...]:
    """Pure aggregation: groups `calls` into `{provider_id, model, period}`
    rows, sorted most-recent period first, then provider_id, then model."""
    buckets: dict[tuple[str, str, str], _Bucket] = {}
    for call in calls:
        if not call.provider_id or not call.model:
            continue
        period = _period_for(call.timestamp)
        if period is None:
            continue
        bucket = buckets.setdefault((call.provider_id, call.model, period), _Bucket())
        if call.trace_id:
            bucket.trace_ids.add(call.trace_id)
        else:
            bucket.untraced += 1
        tokens = _call_tokens(call.token_usage)
        if tokens is not None:
            bucket.reported = True
            bucket.tokens += tokens

    rows = [
        UsageRow(
            provider_id=provider_id,
            model=model,
            period=period,
            state=UsageMeasureState.REPORTED if bucket.reported else UsageMeasureState.UNREPORTED,
            tokens=bucket.tokens if bucket.reported else None,
            turns=len(bucket.trace_ids) + bucket.untraced,
            cost=None,
        )
        for (provider_id, model, period), bucket in buckets.items()
    ]
    rows.sort(key=lambda r: (r.provider_id, r.model))
    rows.sort(key=lambda r: r.period, reverse=True)
    return tuple(rows)
