"""Build version and usage-analytics reads."""

import asyncio

from fastapi import APIRouter, Query

import analytics

router = APIRouter()

git_sha: str = ""


def configure(*, git_sha: str) -> None:
    globals()["git_sha"] = git_sha


@router.get("/api/version")
async def version():
    return {"version": git_sha}


@router.get("/api/analytics")
async def get_analytics(
    start: str = Query(None),
    end: str = Query(None),
    granularity: str = Query(None),
):
    """Usage analytics over a time range. ``start``/``end`` are ISO dates
    ('YYYY-MM-DD'); optional ``granularity`` is hour/day/week/month.

    Retirement (ADR 0009, package E6): the ``llm_calls`` section's
    provider/model/token slice now has a v2 home —
    ``GET /api/v2/surface/runs-usage-analytics``
    (``backend/adapters/runs_adapter.py``'s ``usage_analytics()``, backed
    by ``backend/adapters/usage_analytics.py``), reading the same
    ``llm_call_log`` this endpoint reads via ``analytics.compute_analytics``.
    See the parity mapping table in ``planes-migration-plan.md``'s Package
    E section for the exact legacy-field -> v2-field correspondence and
    the fields with NO v2 target (``sessions``/``turns`` orchestration and
    duration breakdowns, per-token-kind splits) — this endpoint is NOT
    deleted yet: it stays until those gaps are either covered by a future
    ADR or explicitly declared out of scope, per the retirement gate."""
    start_dt, end_dt = analytics.resolve_bounds(start, end)
    bucket = analytics.resolve_granularity(granularity, start_dt, end_dt)
    return await asyncio.to_thread(
        analytics.compute_analytics,
        start_dt,
        end_dt,
        bucket,
    )
