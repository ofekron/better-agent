"""Locks the durable-dismiss fix for restart-seeded background work.

Before this fix, `seed_background_work_after_recovery()` would resurrect an
orphaned `extension_jobs` record as a `STATUS_UNKNOWN` background-work card
on every backend restart, and there was no path that could ever stop it:
`BackgroundWorkRegistry.dismiss()` only removed the in-memory row, and the
`background_work_dismissed` flag `seed_background_work_after_recovery` reads
was never written anywhere. These tests fail on that prior state and pass
once `extension_jobs.mark_background_work_dismissed` and the
`POST /api/background-work/dismiss` route exist and are wired together.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.anyio

import background_work
import background_work_api
import extension_jobs
import paths
from background_work import BackgroundWorkRegistry


def _write_stuck_job_record(owner: str, operation: str, job_id: str) -> None:
    path = extension_jobs.job_path(owner, operation, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "owner": owner,
            "operation": operation,
            "id": job_id,
            "status": "running",
        }),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    """`seed_background_work_after_recovery` and the dismiss route both read
    the module-level singleton, so each test gets an isolated one."""
    registry = BackgroundWorkRegistry()
    monkeypatch.setattr(background_work, "background_work_registry", registry)
    monkeypatch.setattr(background_work_api, "background_work_registry", registry)
    return registry


async def test_mark_background_work_dismissed_stamps_the_record():
    _write_stuck_job_record("requirements", "processed", "job-1")

    assert extension_jobs.mark_background_work_dismissed(
        "requirements", "processed", "job-1"
    ) is True

    record = extension_jobs.read_record_strict("requirements", "processed", "job-1")
    assert record["background_work_dismissed"] is True
    # Independent of status — dismissing must not resurrect or alter it.
    assert record["status"] == "running"


async def test_mark_background_work_dismissed_is_false_for_missing_record():
    assert extension_jobs.mark_background_work_dismissed(
        "requirements", "processed", "does-not-exist"
    ) is False


async def test_dismissed_stuck_job_is_never_reseeded_after_a_simulated_restart(
    _fresh_registry,
):
    _write_stuck_job_record("requirements", "processed", "job-1")

    # Boot 1: the orphaned record surfaces as `unknown`.
    seeded = extension_jobs.seed_background_work_after_recovery()
    assert seeded == 1
    item_id = "extension:requirements:job:processed:job-1"
    assert _fresh_registry.get(item_id) is not None
    assert _fresh_registry.get(item_id)["status"] == background_work.STATUS_UNKNOWN

    # User dismisses it through the real API handler.
    result = await background_work_api.dismiss_background_work({"id": item_id})
    assert result == {"dismissed": True}
    assert _fresh_registry.get(item_id) is None

    # The durable record itself must carry the stamp — not just the
    # in-memory row, which a restart wipes anyway.
    record = extension_jobs.read_record_strict("requirements", "processed", "job-1")
    assert record["background_work_dismissed"] is True

    # Boot 2 (simulated): fresh registry, same on-disk record.
    _fresh_registry.reset()
    reseeded = extension_jobs.seed_background_work_after_recovery()
    assert reseeded == 0
    assert _fresh_registry.get(item_id) is None


async def test_dismiss_endpoint_404s_for_unknown_id(_fresh_registry):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await background_work_api.dismiss_background_work({"id": "extension:nope:job:x:y"})
    assert exc_info.value.status_code == 404


async def test_dismiss_endpoint_requires_a_string_id(_fresh_registry):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await background_work_api.dismiss_background_work({})
    assert exc_info.value.status_code == 400


async def test_dismiss_of_a_core_item_never_touches_extension_jobs(_fresh_registry):
    """A plain core-owned row (not seeded from a durable record) must
    dismiss cleanly without attempting any file mutation."""
    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_CORE,
        owner_id="indexer",
        local_id="scan-1",
        label="Indexing",
        status=background_work.STATUS_RUNNING,
    )

    result = await background_work_api.dismiss_background_work({"id": item_id})

    assert result == {"dismissed": True}
    assert _fresh_registry.get(item_id) is None


async def test_dismiss_never_stamps_a_live_running_item_even_with_seeder_shaped_local_id(
    _fresh_registry,
):
    """An extension picks its own `local_id` when it reports real, live work
    (never STATUS_UNKNOWN). If that id happens to look exactly like the
    seeder's `job:<operation>:<job_id>` shape, dismissing it must NOT touch
    any extension_jobs record — only STATUS_UNKNOWN rows (the seeder's own
    exclusive signature) may. Otherwise a user dismissing an unrelated live
    toast would permanently blind recovery to a genuinely orphaned job."""
    _write_stuck_job_record("requirements", "processed", "job-1")

    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_EXTENSION,
        owner_id="requirements",
        local_id="job:processed:job-1",
        label="Live extraction",
        status=background_work.STATUS_RUNNING,
    )

    result = await background_work_api.dismiss_background_work({"id": item_id})

    assert result == {"dismissed": True}
    assert _fresh_registry.get(item_id) is None
    record = extension_jobs.read_record_strict("requirements", "processed", "job-1")
    assert "background_work_dismissed" not in record


async def test_non_dismissible_item_returns_409(_fresh_registry):
    from fastapi import HTTPException

    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_CORE,
        owner_id="indexer",
        local_id="scan-1",
        label="Indexing",
        status=background_work.STATUS_RUNNING,
        dismissible=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await background_work_api.dismiss_background_work({"id": item_id})
    assert exc_info.value.status_code == 409
    assert _fresh_registry.get(item_id) is not None


async def test_double_dismiss_of_the_same_id_404s_on_the_second_call(_fresh_registry):
    from fastapi import HTTPException

    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_CORE,
        owner_id="indexer",
        local_id="scan-1",
        label="Indexing",
        status=background_work.STATUS_RUNNING,
    )

    first = await background_work_api.dismiss_background_work({"id": item_id})
    assert first == {"dismissed": True}

    with pytest.raises(HTTPException) as exc_info:
        await background_work_api.dismiss_background_work({"id": item_id})
    assert exc_info.value.status_code == 404
