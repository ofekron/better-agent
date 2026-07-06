"""Usage analytics — read-only aggregation for the analytics page.

Reads only (single source of truth untouched):
  - session_store.list_sessions()       -> root session summaries
  - trace_collector.iter_trace_index()  -> per-turn index entries
  - config_store.list_providers()       -> provider id -> kind/name

What is counted where:
  - Sessions: root summaries that represent REAL usage — not internal
    working-mode sessions (requirement analysis, search workers, tagger…)
    and not empty stubs (0 messages). Counted by created_at.
  - Turns: per-turn trace entries whose session_id is one of those real
    sessions (background-agent turns and orphaned worker traces are
    excluded). Counted by the trace timestamp; carry duration.

Token totals are intentionally NOT aggregated. The persisted per-turn
token_usage is cumulative-context-sized (the known "context window
math" issue), so summing across turns over-counts by orders of
magnitude. Counts and durations are reliable; token sums are not.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import config_store
import llm_call_log
import native_session_miner
import native_transcript_index
import session_store
import trace_collector

logger = logging.getLogger(__name__)

DEFAULT_RANGE_DAYS = 30
ANALYTICS_ALL_START = datetime(2000, 1, 1)
NATIVE_ANALYTICS_SQL_TIMEOUT_SECONDS = 15.0


# ── datetime helpers ────────────────────────────────────────────────────


def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO timestamp or date into a naive local datetime.

    Handles trailing ``Z`` and explicit offsets by dropping the tzinfo
    after converting to local time. Returns None on garbage input.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        dt = value.astimezone()
    else:
        dt = value
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_bounds(
    start: Optional[str], end: Optional[str]
) -> tuple[datetime, datetime]:
    """Resolve analytics range bounds from optional date inputs.

    A date-only ``end`` ('YYYY-MM-DD') expands to end-of-day so the last
    day is fully included. Defaults: end = now, start = all-time lower bound.
    """
    now = datetime.now()
    end_dt = _parse_dt(end) if end else now
    if end_dt is None:
        end_dt = now
    if end and len(end.strip()) <= 10:
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_dt = _parse_dt(start) if start else ANALYTICS_ALL_START
    if start_dt is None:
        start_dt = end_dt - timedelta(days=DEFAULT_RANGE_DAYS)
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    return start_dt, end_dt


def _choose_granularity(start: datetime, end: datetime) -> str:
    span_days = (end - start).days
    if span_days <= 2:
        return "hour"
    if span_days <= 60:
        return "day"
    if span_days <= 365:
        return "week"
    return "month"


def _bucket_label(dt: datetime, granularity: str) -> str:
    if granularity == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "week":
        monday = dt - timedelta(days=dt.weekday())
        return monday.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m")


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


# ── session filter ──────────────────────────────────────────────────────


def _is_real_session(s: dict) -> bool:
    """A session that represents actual user usage: not an internal
    working-mode session (requirement analysis, search workers, tagger,
    prompt-engineering, …) and has at least one message."""
    if s.get("working_mode"):
        return False
    return (s.get("message_count") or 0) > 0


# ── core aggregation ────────────────────────────────────────────────────


def aggregate(
    sessions: Iterable[dict],
    traces: Iterable[dict],
    llm_calls: Iterable[dict],
    provider_map: dict,
    start: datetime,
    end: datetime,
    native_conversations: Optional[Iterable[dict]] = None,
) -> dict:
    """Pure aggregation over raw native conversations + BA supplements."""
    granularity = _choose_granularity(start, end)

    sid_attr: dict[str, tuple[str, str, str, str]] = {}
    real_sessions = [s for s in sessions if _is_real_session(s)]
    for s in real_sessions:
        sid = s.get("id")
        if not sid:
            continue
        prov = provider_map.get(s.get("provider_id")) or {}
        kind = prov.get("kind") or "unknown"
        name = prov.get("name") or kind
        pkey = s.get("provider_id") or "unknown"
        model = s.get("model") or "unknown"
        sid_attr[sid] = (pkey, kind, name, model)

    native_items = list(native_conversations or [])
    native_session_ids = {
        item.get("sid") for item in native_items if item.get("sid")
    }

    def _session_attr(s: dict) -> tuple[str, str, str, str]:
        prov = provider_map.get(s.get("provider_id")) or {}
        kind = prov.get("kind") or "unknown"
        name = prov.get("name") or kind
        return (
            s.get("provider_id") or f"ba:{kind}",
            kind,
            name,
            s.get("model") or "unknown",
        )

    def _native_attr(item: dict) -> tuple[str, str, str, str]:
        kind = item.get("provider_kind") or item.get("tag") or "unknown"
        return (
            item.get("provider_key") or f"native:{kind}",
            kind,
            item.get("provider_name") or _provider_name_for_kind(kind, provider_map),
            item.get("model") or "unknown",
        )

    # ---- sessions (counted by created_at) ----
    sess_series: dict[str, int] = defaultdict(int)
    sess_total = 0
    messages_total = 0
    by_provider: dict[str, dict] = defaultdict(
        lambda: {"kind": "", "name": "", "count": 0}
    )
    by_model: dict[tuple, dict] = defaultdict(
        lambda: {"kind": "", "model": "", "count": 0}
    )
    by_orch: dict[str, int] = defaultdict(int)

    for item in native_items:
        created = _parse_dt(item.get("created_at"))
        if not created or created < start or created > end:
            continue
        pkey, kind, name, model = _native_attr(item)
        mode = item.get("orchestration_mode") or "native"

        sess_total += 1
        messages_total += item.get("message_count") or 0
        sess_series[_bucket_label(created, granularity)] += 1

        bp = by_provider[pkey]
        bp["kind"] = kind
        bp["name"] = name
        bp["count"] += 1
        bm = by_model[(kind, model)]
        bm["kind"] = kind
        bm["model"] = model
        bm["count"] += 1
        by_orch[mode] += 1

    for s in real_sessions:
        if s.get("id") in native_session_ids:
            continue
        created = _parse_dt(s.get("created_at"))
        if not created or created < start or created > end:
            continue
        pkey, kind, name, model = _session_attr(s)
        mode = s.get("orchestration_mode") or "unknown"

        sess_total += 1
        messages_total += s.get("message_count") or 0
        sess_series[_bucket_label(created, granularity)] += 1

        bp = by_provider[pkey]
        bp["kind"] = kind
        bp["name"] = name
        bp["count"] += 1
        bm = by_model[(kind, model)]
        bm["kind"] = kind
        bm["model"] = model
        bm["count"] += 1
        by_orch[mode] += 1

    # ---- turns (real-session traces in range; carry duration) ----
    def _turn_bucket() -> dict:
        return {"count": 0, "duration_ms_sum": 0.0}

    turn_series: dict[str, dict] = defaultdict(_turn_bucket)
    turn_total = 0
    durations: list[float] = []
    t_by_provider: dict[str, dict] = defaultdict(
        lambda: {"kind": "", "name": "", "turns": 0}
    )
    t_by_model: dict[tuple, dict] = defaultdict(
        lambda: {"kind": "", "model": "", "turns": 0}
    )

    for item in native_items:
        pkey, kind, name, model = _native_attr(item)
        for turn in item.get("turns") or []:
            ts = _parse_dt(turn.get("timestamp"))
            if not ts or ts < start or ts > end:
                continue

            turn_total += 1
            b = turn_series[_bucket_label(ts, granularity)]
            b["count"] += 1

            bp = t_by_provider[pkey]
            bp["kind"] = kind
            bp["name"] = name
            bp["turns"] += 1
            bm = t_by_model[(kind, model)]
            bm["kind"] = kind
            bm["model"] = model
            bm["turns"] += 1

    for tr in traces:
        ts = _parse_dt(tr.get("timestamp"))
        if not ts or ts < start or ts > end:
            continue
        if tr.get("session_id") in native_session_ids:
            continue
        attr = sid_attr.get(tr.get("session_id"))
        if attr is None:
            continue  # internal / worker / orphaned trace — not user usage
        pkey, kind, name, model = attr
        dur = tr.get("duration_ms")
        dur_f = float(dur) if isinstance(dur, (int, float)) else 0.0

        turn_total += 1
        b = turn_series[_bucket_label(ts, granularity)]
        b["count"] += 1
        b["duration_ms_sum"] += dur_f
        if dur_f:
            durations.append(dur_f)

        bp = t_by_provider[pkey]
        bp["kind"] = kind
        bp["name"] = name
        bp["turns"] += 1
        bm = t_by_model[(kind, model)]
        bm["kind"] = kind
        bm["model"] = model
        bm["turns"] += 1

    duration_avg = round(sum(durations) / len(durations), 1) if durations else 0.0

    # ---- LLM calls (single append-only log; all provider/internal call sites) ----
    def _call_bucket() -> dict:
        return {
            "count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": 0,
        }

    call_series: dict[str, dict] = defaultdict(_call_bucket)
    calls_by_provider: dict[str, dict] = defaultdict(
        lambda: {"provider_id": "", "kind": "", "name": "", "calls": 0, "total_tokens": 0}
    )
    calls_by_model: dict[tuple, dict] = defaultdict(
        lambda: {"kind": "", "model": "", "calls": 0, "total_tokens": 0}
    )
    calls_by_source: dict[str, dict] = defaultdict(
        lambda: {"source": "", "calls": 0, "total_tokens": 0}
    )
    calls_by_reason: dict[str, dict] = defaultdict(
        lambda: {"reason": "", "calls": 0, "total_tokens": 0}
    )
    recent_calls: list[dict] = []
    call_total = 0
    call_token_totals = _call_bucket()

    for call in llm_calls:
        ts = _parse_dt(call.get("timestamp"))
        if not ts or ts < start or ts > end:
            continue
        usage = call.get("token_usage") if isinstance(call.get("token_usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0) or (
            input_tokens + output_tokens
        )
        kind = call.get("provider_kind") or "unknown"
        provider_id = call.get("provider_id") or "unknown"
        name = call.get("provider_name") or provider_map.get(provider_id, {}).get("name") or kind
        model = call.get("model") or "unknown"
        source = call.get("source") or "unknown"
        reason = call.get("reason") or "unknown"

        call_total += 1
        for key, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_input_tokens", cache_read),
            ("cache_creation_input_tokens", cache_creation),
            ("total_tokens", total_tokens),
        ):
            call_token_totals[key] += value
        bucket = call_series[_bucket_label(ts, granularity)]
        bucket["count"] += 1
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_input_tokens"] += cache_read
        bucket["cache_creation_input_tokens"] += cache_creation
        bucket["total_tokens"] += total_tokens

        bp = calls_by_provider[provider_id]
        bp["provider_id"] = provider_id
        bp["kind"] = kind
        bp["name"] = name
        bp["calls"] += 1
        bp["total_tokens"] += total_tokens

        bm = calls_by_model[(kind, model)]
        bm["kind"] = kind
        bm["model"] = model
        bm["calls"] += 1
        bm["total_tokens"] += total_tokens

        bs = calls_by_source[source]
        bs["source"] = source
        bs["calls"] += 1
        bs["total_tokens"] += total_tokens

        br = calls_by_reason[reason]
        br["reason"] = reason
        br["calls"] += 1
        br["total_tokens"] += total_tokens

        recent_calls.append({
            "id": call.get("id"),
            "timestamp": call.get("timestamp"),
            "source": source,
            "reason": reason,
            "provider_id": provider_id,
            "provider_kind": kind,
            "provider_name": name,
            "model": model,
            "reasoning_effort": call.get("reasoning_effort"),
            "app_session_id": call.get("app_session_id"),
            "provider_session_id": call.get("provider_session_id"),
            "trace_id": call.get("trace_id"),
            "prompt_preview": call.get("prompt_preview") or "",
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "total_tokens": total_tokens,
            },
            "success": call.get("success"),
            "error": call.get("error"),
        })

    recent_calls.sort(key=lambda c: c.get("timestamp") or "", reverse=True)

    return {
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "granularity": granularity,
        },
        "providers": [
            {"id": pid, "name": p.get("name") or p.get("kind") or "unknown",
             "kind": p.get("kind") or "unknown"}
            for pid, p in provider_map.items()
        ],
        "sessions": {
            "total": sess_total,
            "messages_total": messages_total,
            "series": [{"t": t, "count": c} for t, c in sorted(sess_series.items())],
            "by_provider": _sorted(by_provider, "count"),
            "by_model": _sorted(by_model, "count"),
            "by_orchestration": [
                {"mode": m, "count": c}
                for m, c in sorted(by_orch.items(), key=lambda kv: kv[1], reverse=True)
            ],
        },
        "turns": {
            "total": turn_total,
            "series": [
                {"t": t, "count": b["count"], "duration_ms": round(b["duration_ms_sum"], 1)}
                for t, b in sorted(turn_series.items())
            ],
            "by_provider": _sorted(t_by_provider, "turns"),
            "by_model": _sorted(t_by_model, "turns"),
            "duration_avg_ms": duration_avg,
            "duration_p50_ms": round(_median(durations), 1),
        },
        "llm_calls": {
            "total": call_total,
            "token_usage": {
                k: v for k, v in call_token_totals.items()
                if k != "count"
            },
            "series": [
                {"t": t, **b}
                for t, b in sorted(call_series.items())
            ],
            "by_provider": _sorted(calls_by_provider, "calls"),
            "by_model": _sorted(calls_by_model, "calls"),
            "by_source": _sorted(calls_by_source, "calls"),
            "by_reason": _sorted(calls_by_reason, "calls")[:12],
            "recent": recent_calls[:100],
        },
    }


def _sorted(group_map: dict, key: str) -> list[dict]:
    return [
        dict(item)
        for item in sorted(
            group_map.values(), key=lambda d: d.get(key, 0), reverse=True
        )
    ]


def _provider_name_for_kind(kind: str, provider_map: dict) -> str:
    for provider in provider_map.values():
        if provider.get("kind") == kind and provider.get("name"):
            return provider["name"]
    return kind or "unknown"


def _native_conversations_from_index(start: datetime, end: datetime) -> list[dict]:
    state = native_transcript_index.quick_state()
    if not state.get("schema_ok") or not state.get("covered"):
        return _native_conversations_from_raw(start, end)

    start_z = _utc_z(start)
    end_z = _utc_z(end)
    result = native_transcript_index.run_readonly_sql(
        """
        WITH range_paths AS (
            SELECT DISTINCT path
            FROM native_element_meta
            WHERE element_kind = 'user_prompt'
              AND ts_utc >= ?
              AND ts_utc <= ?
        ),
        first_prompts AS (
            SELECT path, MIN(ts_utc) AS created_at
            FROM native_element_meta
            WHERE element_kind = 'user_prompt'
              AND path IN (SELECT path FROM range_paths)
            GROUP BY path
        ),
        metadata AS (
            SELECT
                path,
                COALESCE(MAX(sid), '') AS sid,
                COALESCE(MAX(cwd), '') AS cwd,
                COALESCE(MAX(tag), 'unknown') AS tag,
                COUNT(*) AS message_count
            FROM native_element_meta
            WHERE path IN (SELECT path FROM range_paths)
              AND element_kind IN ('user_prompt', 'assistant_text')
              AND ts_utc IS NOT NULL
              AND ts_utc != ''
            GROUP BY path
        )
        SELECT
            fp.path,
            COALESCE(m.sid, '') AS sid,
            COALESCE(m.cwd, '') AS cwd,
            COALESCE(m.tag, 'unknown') AS tag,
            fp.created_at,
            COALESCE(m.message_count, 0) AS message_count
        FROM first_prompts fp
        LEFT JOIN metadata m ON m.path = fp.path
        """,
        (start_z, end_z),
        timeout_s=NATIVE_ANALYTICS_SQL_TIMEOUT_SECONDS,
    )
    if result.get("error"):
        logger.warning("native analytics query failed: %s", result.get("error"))
        return []
    columns = result.get("columns") or []
    conversations: dict[str, dict] = {
        row["path"]: {
            "id": f"native:{row['path']}",
            "sid": row.get("sid") or "",
            "cwd": row.get("cwd") or "",
            "provider_kind": row.get("tag") or "unknown",
            "provider_key": f"native:{row.get('tag') or 'unknown'}",
            "model": "unknown",
            "orchestration_mode": "native",
            "created_at": row.get("created_at") or "",
            "message_count": row.get("message_count") or 0,
            "turns": [],
        }
        for row in (dict(zip(columns, raw)) for raw in result.get("rows") or [])
        if row.get("path")
    }
    if not conversations:
        return []

    turns_result = native_transcript_index.run_readonly_sql(
        """
        SELECT path, ts_utc
        FROM native_element_meta
        WHERE element_kind = 'user_prompt'
          AND ts_utc >= ?
          AND ts_utc <= ?
        ORDER BY path, ts_utc, rowid
        """,
        (start_z, end_z),
        timeout_s=NATIVE_ANALYTICS_SQL_TIMEOUT_SECONDS,
    )
    if turns_result.get("error"):
        logger.warning("native analytics turns query failed: %s", turns_result.get("error"))
        return list(conversations.values())
    turn_columns = turns_result.get("columns") or []
    for raw in turns_result.get("rows") or []:
        row = dict(zip(turn_columns, raw))
        item = conversations.get(row.get("path"))
        if item is not None:
            item["turns"].append({"timestamp": row.get("ts_utc") or ""})
    return list(conversations.values())


def _native_conversations_from_raw(start: datetime, end: datetime) -> list[dict]:
    conversations: list[dict] = []
    for candidate in native_session_miner.iter_all_native_candidates():
        elements = candidate.parse_elements()
        user_prompts = []
        message_count = 0
        for element in elements:
            if element.kind not in {"user_prompt", "assistant_text"}:
                continue
            ts = _parse_dt(element.timestamp)
            if not ts:
                continue
            message_count += 1
            if element.kind == "user_prompt":
                user_prompts.append(ts)
        if not user_prompts:
            continue
        turns = [
            {"timestamp": _utc_z(ts)}
            for ts in user_prompts
            if start <= ts <= end
        ]
        if not turns:
            continue
        provider_kind = candidate.format or "unknown"
        created_at = min(user_prompts)
        path = str(candidate.transcript)
        conversations.append({
            "id": f"native:{path}",
            "sid": candidate.sid or "",
            "cwd": candidate.cwd or "",
            "provider_kind": provider_kind,
            "provider_key": f"native:{provider_kind}",
            "model": "unknown",
            "orchestration_mode": "native",
            "created_at": _utc_z(created_at),
            "message_count": message_count,
            "turns": turns,
        })
    return conversations


# ── wiring: fetch live data and aggregate ───────────────────────────────


def compute_analytics(start: datetime, end: datetime) -> dict:
    """Read live data from the stores and aggregate over [start, end]."""
    sessions = session_store.list_sessions()
    traces = list(trace_collector.iter_trace_index())
    llm_calls = list(llm_call_log.iter_calls())
    prov_state = config_store.list_providers()
    provider_map = {
        p["id"]: p for p in prov_state.get("providers", []) if p.get("id")
    }
    return aggregate(
        sessions,
        traces,
        llm_calls,
        provider_map,
        start,
        end,
        _native_conversations_from_index(start, end),
    )
