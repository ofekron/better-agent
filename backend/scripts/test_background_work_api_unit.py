"""Unit coverage for the branches `test_background_work_dismiss.py` leaves
open in `background_work_api.py`: the first-paint `GET` route, the pure
`_seeded_job_identity` parser (every shape branch), and the two dismiss
side-channel branches — a `STATUS_UNKNOWN` extension row whose id is not the
seeder's shape (stamp skipped), and a seeder-shaped row whose durable record
is already gone (stamp attempted, warn logged)."""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.anyio

import background_work
import background_work_api
import extension_jobs
from background_work import BackgroundWorkRegistry


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    """The routes read the module-level singleton; each test gets an isolated
    registry so a dismiss in one test cannot bleed into another."""
    registry = BackgroundWorkRegistry()
    monkeypatch.setattr(background_work, "background_work_registry", registry)
    monkeypatch.setattr(background_work_api, "background_work_registry", registry)
    return registry


# -- _seeded_job_identity: pure parser, every branch ----------------------


def test_seeded_identity_recovers_owner_operation_job():
    assert background_work_api._seeded_job_identity(
        "extension:requirements:job:processed:job-1"
    ) == ("requirements", "processed", "job-1")


def test_seeded_identity_rejects_wrong_owner_prefix():
    # parts[0] is not OWNER_EXTENSION -> None
    assert background_work_api._seeded_job_identity("core:o:job:op:id") is None


def test_seeded_identity_rejects_too_few_top_level_parts():
    # split(":", 2) yields fewer than 3 parts -> None
    assert background_work_api._seeded_job_identity("extension:owner") is None


def test_seeded_identity_rejects_missing_job_marker():
    # local_id parses to 3 parts but the first is not "job" -> None
    assert background_work_api._seeded_job_identity(
        "extension:owner:task:op:id"
    ) is None


def test_seeded_identity_rejects_too_few_job_parts():
    # local_id does not split into job:<operation>:<job_id> -> None
    assert background_work_api._seeded_job_identity(
        "extension:owner:job:op"
    ) is None


# -- get_background_work: first-paint route --------------------------------


async def test_get_background_work_returns_the_registry_snapshot(_fresh_registry):
    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_CORE,
        owner_id="indexer",
        local_id="scan-1",
        label="Indexing",
    )

    result = await background_work_api.get_background_work()

    assert result == _fresh_registry.snapshot()
    assert any(item["id"] == item_id for item in result["items"])


# -- dismiss: the two STATUS_UNKNOWN side-channel branches -----------------


async def test_dismiss_skips_stamping_when_status_unknown_id_is_not_seeder_shaped(
    _fresh_registry, monkeypatch
):
    """A `STATUS_UNKNOWN` extension row is the seeder's exclusive signature,
    so the gate opens — but if the owner-chosen local_id is not the seeder's
    `job:<operation>:<job_id>` shape there is no durable record to stamp, and
    the stamp must not even be attempted."""
    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_EXTENSION,
        owner_id="covunit",
        local_id="not-a-job-shape",
        label="Orphan",
        status=background_work.STATUS_UNKNOWN,
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError("mark_background_work_dismissed must not run for a non-seeder-shaped id")

    monkeypatch.setattr(extension_jobs, "mark_background_work_dismissed", _must_not_run)

    result = await background_work_api.dismiss_background_work({"id": item_id})

    assert result == {"dismissed": True}
    assert _fresh_registry.get(item_id) is None


async def test_dismiss_warns_when_seeder_shaped_record_is_already_gone(
    _fresh_registry, caplog
):
    """The recovery seeder's own id shape opens the stamp path even for a row
    a later restart no longer has a durable record for. The stamp then returns
    False and the route logs a warning rather than silently dropping it."""
    item_id = _fresh_registry.report(
        owner_kind=background_work.OWNER_EXTENSION,
        owner_id="covgone",
        local_id="job:processed:job-gone",
        label="Orphan",
        status=background_work.STATUS_UNKNOWN,
    )
    # No durable extension_jobs record is written, so the stamp must miss.

    with caplog.at_level(logging.WARNING, logger="uvicorn"):
        result = await background_work_api.dismiss_background_work({"id": item_id})

    assert result == {"dismissed": True}
    assert _fresh_registry.get(item_id) is None
    assert any(
        "background_work_dismiss_not_stamped" in record.getMessage()
        for record in caplog.records
    )
