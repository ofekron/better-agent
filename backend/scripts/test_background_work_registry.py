"""Locks the BackgroundWorkRegistry mutation funnel.

The registry is the single write path for user-visible background work, so
the invariants proven here are the ones every producer depends on: stable
ids, idempotent terminal transitions, caps enforced in the funnel (not at the
broadcast boundary, which never sees non-global events), monotonic `seq` for
the client staleness guard, and a mutation that survives a closed broadcast
channel.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.anyio

import background_work
from background_work import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    BackgroundWorkRegistry,
    BackgroundWorkRejected,
)


class _RecordingCoordinator:
    """Captures broadcasts instead of touching a real WS fan-out."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self._fail_with = fail_with

    async def broadcast_global(self, event_type: str, data: dict) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.events.append((event_type, data))


async def _bound_registry(coordinator: _RecordingCoordinator) -> BackgroundWorkRegistry:
    registry = BackgroundWorkRegistry()
    registry.bind(coordinator, asyncio.get_running_loop())
    return registry


async def _settle() -> None:
    """Broadcasts are scheduled as tasks after the lock is released."""
    for _ in range(3):
        await asyncio.sleep(0)


def _report(registry: BackgroundWorkRegistry, local_id: str = "job", **kwargs) -> str:
    params = {
        "owner_kind": "core",
        "owner_id": "tests",
        "local_id": local_id,
        "label": "Indexing repo",
    }
    params.update(kwargs)
    return registry.report(**params)


async def test_report_assigns_stable_id_and_emits_one_delta():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)

    item_id = _report(registry)
    await _settle()

    assert item_id == "core:tests:job"
    assert len(coordinator.events) == 1
    event_type, data = coordinator.events[0]
    assert event_type == background_work.EVENT_TYPE
    assert data["item"]["id"] == item_id
    assert data["item"]["status"] == STATUS_RUNNING
    assert data["epoch"] == registry.epoch


async def test_finish_is_idempotent():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)
    await _settle()
    coordinator.events.clear()

    assert registry.finish(item_id) is True
    assert registry.finish(item_id) is False
    await _settle()

    assert len(coordinator.events) == 1
    assert coordinator.events[0][1]["item"]["status"] == STATUS_SUCCEEDED


async def test_update_cannot_resurrect_finished_work():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)
    registry.finish(item_id, status=STATUS_FAILED, error="boom")
    await _settle()
    coordinator.events.clear()

    assert registry.update(item_id, phase="still going") is False
    await _settle()

    assert coordinator.events == []
    assert registry.get(item_id)["status"] == STATUS_FAILED
    assert registry.get(item_id)["error"] == "boom"


async def test_owner_item_cap_rejects_and_leaves_registry_untouched():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    for index in range(background_work._MAX_ITEMS_PER_OWNER):
        _report(registry, local_id=f"job-{index}")
    await _settle()
    before = registry.snapshot()

    with pytest.raises(BackgroundWorkRejected):
        _report(registry, local_id="one-too-many")
    await _settle()

    after = registry.snapshot()
    assert len(after["items"]) == len(before["items"])
    assert after["seq"] == before["seq"]
    assert registry.get("core:tests:one-too-many") is None


async def test_reporting_an_existing_id_again_does_not_consume_cap():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    for index in range(background_work._MAX_ITEMS_PER_OWNER):
        _report(registry, local_id=f"job-{index}")

    # Re-reporting an id already held is an update of that row, so it must
    # not count as a new allocation against the cap.
    _report(registry, local_id="job-0", label="Indexing repo (resumed)")
    await _settle()

    assert registry.get("core:tests:job-0")["label"] == "Indexing repo (resumed)"


async def test_mutation_applies_even_when_broadcast_channel_is_closed():
    coordinator = _RecordingCoordinator(
        fail_with=RuntimeError("global broadcast scheduling is closed")
    )
    registry = await _bound_registry(coordinator)

    item_id = _report(registry)
    registry.finish(item_id)
    await _settle()

    assert registry.get(item_id)["status"] == STATUS_SUCCEEDED


async def test_seq_is_monotonic_across_mutations():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)
    registry.update(item_id, phase="parsing")
    registry.finish(item_id)
    await _settle()

    seqs = [data["item"]["seq"] for _, data in coordinator.events if "item" in data]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_reset_reepochs_so_clients_discard_stale_state():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    _report(registry)
    first_epoch = registry.epoch

    registry.reset()
    await _settle()

    assert registry.epoch != first_epoch
    assert registry.snapshot()["items"] == []
    assert coordinator.events[-1][1] == {"epoch": registry.epoch, "cleared": True}


async def test_indeterminate_progress_never_becomes_a_percentage():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)

    indeterminate = _report(registry, local_id="a", progress=None)
    counted = _report(registry, local_id="b", progress={"completed": 7, "total": None})
    await _settle()

    assert registry.get(indeterminate)["progress"] is None
    assert registry.get(counted)["progress"] == {
        "completed": 7, "total": None, "unit": "",
    }


async def test_progress_completed_is_clamped_to_total():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry, progress={"completed": 99, "total": 10})
    await _settle()

    assert registry.get(item_id)["progress"]["completed"] == 10


async def test_owner_supplied_text_is_clamped_not_dropped():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry, label="x" * 5000, detail="y" * 5000)
    await _settle()

    item = registry.get(item_id)
    assert len(item["label"]) == background_work._MAX_LABEL
    assert len(item["detail"]) == background_work._MAX_DETAIL


async def test_rejects_unsupported_enums_and_actions():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)

    with pytest.raises(BackgroundWorkRejected):
        _report(registry, owner_kind="root")
    with pytest.raises(BackgroundWorkRejected):
        _report(registry, status="exploded")
    with pytest.raises(BackgroundWorkRejected):
        _report(registry, actions=[{"kind": "run_shell", "target": "rm -rf /"}])
    with pytest.raises(BackgroundWorkRejected):
        _report(registry, actions=[{"kind": "open_url", "target": "javascript:alert(1)"}])


async def test_finish_rejects_a_non_terminal_status():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)

    with pytest.raises(BackgroundWorkRejected):
        registry.finish(item_id, status=STATUS_RUNNING)


async def test_update_rejects_terminal_status_to_keep_one_terminal_path():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)

    with pytest.raises(BackgroundWorkRejected):
        registry.update(item_id, status=STATUS_SUCCEEDED)


async def test_unknown_status_is_not_terminal_and_stays_visible():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry, status=STATUS_UNKNOWN)
    await _settle()

    # A restart-seeded row must remain resolvable by its owner rather than
    # being treated as finished work.
    assert registry.get(item_id)["finished_at"] is None
    assert registry.update(item_id, phase="resumed") is True


async def test_terminal_retention_cap_evicts_fifo_and_keeps_open_work():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    open_id = _report(registry, local_id="open-work")
    for index in range(background_work._TERMINAL_RETENTION_CAP + 5):
        finished = registry.report(
            owner_kind="core",
            owner_id=f"owner-{index}",
            local_id="job",
            label="done",
        )
        registry.finish(finished)
    await _settle()

    items = registry.snapshot()["items"]
    terminal = [i for i in items if i["status"] == STATUS_SUCCEEDED]
    assert len(terminal) == background_work._TERMINAL_RETENTION_CAP
    assert any(i["id"] == open_id for i in items)


async def test_finish_owner_finalizes_without_a_timer():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    first = registry.report(
        owner_kind="extension", owner_id="ext.a", local_id="1", label="a",
    )
    second = registry.report(
        owner_kind="extension", owner_id="ext.a", local_id="2", label="b",
    )
    other = registry.report(
        owner_kind="extension", owner_id="ext.b", local_id="1", label="c",
    )

    finalized = registry.finish_owner("extension", "ext.a", error="extension disabled")
    await _settle()

    assert finalized == 2
    assert registry.get(first)["status"] == STATUS_CANCELLED
    assert registry.get(second)["status"] == STATUS_CANCELLED
    assert registry.get(other)["status"] == STATUS_RUNNING


async def test_dismiss_removes_the_row_and_announces_the_removal():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry)
    await _settle()
    coordinator.events.clear()

    assert registry.dismiss(item_id) is True
    await _settle()

    assert registry.get(item_id) is None
    assert coordinator.events[-1][1]["removed_id"] == item_id


async def test_non_dismissible_item_cannot_be_dismissed():
    coordinator = _RecordingCoordinator()
    registry = await _bound_registry(coordinator)
    item_id = _report(registry, dismissible=False)

    assert registry.dismiss(item_id) is False
    assert registry.get(item_id) is not None


async def test_snapshot_is_self_sufficient_without_any_broadcast():
    # Mirrors degraded boot: no coordinator bound yet, so nothing can be
    # broadcast, but first paint must still be served.
    registry = BackgroundWorkRegistry()
    item_id = _report(registry)

    snapshot = registry.snapshot()
    assert snapshot["epoch"] == registry.epoch
    assert [i["id"] for i in snapshot["items"]] == [item_id]
