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
    ('YYYY-MM-DD'); optional ``granularity`` is hour/day/week/month."""
    start_dt, end_dt = analytics.resolve_bounds(start, end)
    bucket = analytics.resolve_granularity(granularity, start_dt, end_dt)
    return await asyncio.to_thread(
        analytics.compute_analytics,
        start_dt,
        end_dt,
        bucket,
    )
