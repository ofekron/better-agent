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

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Optional

import config_store
import session_store
import trace_collector

DEFAULT_RANGE_DAYS = 30


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


def resolve_bounds(
    start: Optional[str], end: Optional[str]
) -> tuple[datetime, datetime]:
    """Resolve analytics range bounds from optional date inputs.

    A date-only ``end`` ('YYYY-MM-DD') expands to end-of-day so the last
    day is fully included. Defaults: end = now, start = end - 30 days.
    """
    now = datetime.now()
    end_dt = _parse_dt(end) if end else now
    if end_dt is None:
        end_dt = now
    if end and len(end.strip()) <= 10:
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_dt = _parse_dt(start) if start else (end_dt - timedelta(days=DEFAULT_RANGE_DAYS))
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
    provider_map: dict,
    start: datetime,
    end: datetime,
) -> dict:
    """Pure aggregation over raw session summaries + trace index entries."""
    granularity = _choose_granularity(start, end)

    # real sessions only; build session_id -> (provider_key, kind, name, model)
    # from ALL real sessions (not just in-range) so a turn whose session was
    # created before the range still attributes correctly.
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

    for s in real_sessions:
        created = _parse_dt(s.get("created_at"))
        if not created or created < start or created > end:
            continue
        pkey, kind, name, model = sid_attr[s["id"]]
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

    for tr in traces:
        ts = _parse_dt(tr.get("timestamp"))
        if not ts or ts < start or ts > end:
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
    }


def _sorted(group_map: dict, key: str) -> list[dict]:
    return [
        dict(item)
        for item in sorted(
            group_map.values(), key=lambda d: d.get(key, 0), reverse=True
        )
    ]


# ── wiring: fetch live data and aggregate ───────────────────────────────


def compute_analytics(start: datetime, end: datetime) -> dict:
    """Read live data from the stores and aggregate over [start, end]."""
    sessions = session_store.list_sessions()
    traces = list(trace_collector.iter_trace_index())
    prov_state = config_store.list_providers()
    provider_map = {
        p["id"]: p for p in prov_state.get("providers", []) if p.get("id")
    }
    return aggregate(sessions, traces, provider_map, start, end)
