from __future__ import annotations

import pytest

import misc_api
from misc_api import configure, get_analytics, version

pytestmark = pytest.mark.anyio


class TestVersion:
    async def test_default_version_empty(self) -> None:
        configure(git_sha="")
        assert (await version()) == {"version": ""}

    async def test_configured_version_returned(self) -> None:
        configure(git_sha="abc123")
        assert (await version()) == {"version": "abc123"}


class TestGetAnalytics:
    async def test_resolves_and_computes(self, monkeypatch) -> None:
        bounds = ("2026-01-01", "2026-02-01")
        calls: dict = {}

        def fake_resolve_bounds(start, end):
            calls["bounds"] = (start, end)
            return bounds

        def fake_resolve_granularity(granularity, start_dt, end_dt):
            calls["granularity"] = (granularity, start_dt, end_dt)
            return "day"

        def fake_compute(start_dt, end_dt, bucket):
            calls["compute"] = (start_dt, end_dt, bucket)
            return {"rows": [], "bucket": bucket}

        monkeypatch.setattr(misc_api.analytics, "resolve_bounds", fake_resolve_bounds)
        monkeypatch.setattr(misc_api.analytics, "resolve_granularity", fake_resolve_granularity)
        monkeypatch.setattr(misc_api.analytics, "compute_analytics", fake_compute)

        result = await get_analytics(start="2026-01-01", end="2026-02-01", granularity="day")

        assert result == {"rows": [], "bucket": "day"}
        assert calls["bounds"] == ("2026-01-01", "2026-02-01")
        assert calls["granularity"] == ("day", "2026-01-01", "2026-02-01")
        assert calls["compute"] == ("2026-01-01", "2026-02-01", "day")

    async def test_null_params_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr(misc_api.analytics, "resolve_bounds", lambda s, e: (s, e))
        monkeypatch.setattr(
            misc_api.analytics, "resolve_granularity", lambda g, s, e: "hour"
        )
        monkeypatch.setattr(misc_api.analytics, "compute_analytics", lambda s, e, b: {"ok": True})

        result = await get_analytics()
        assert result == {"ok": True}
