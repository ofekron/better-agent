"""Proof that removed session features stay absent while legacy data loads."""
from __future__ import annotations

import inspect
import os
import shutil
import sys

import _test_home
from _test_routes import walk_routes

_TMP_HOME = _test_home.isolate("bc-test-removed-feature-legacy-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import app_lifecycle  # noqa: E402
import config_store  # noqa: E402
import session_store  # noqa: E402


def main_test() -> int:
    provider = config_store.add_provider({
        "name": "Legacy load test",
        "kind": "claude",
        "mode": "subscription",
    })
    config_store.set_default_provider(provider["id"])

    route_paths = {route.path for route in walk_routes(main.app.routes)}
    assert not any("/adv_sync" in path for path in route_paths)

    startup_source = inspect.getsource(app_lifecycle.on_startup)
    assert "adv_sync_overlay_recovery" not in startup_source

    manager = main.session_manager
    coordinator = main.coordinator
    parent = session_store.create_session(
        name="legacy-parent",
        cwd="/tmp",
        provider_id=provider["id"],
        runner="native",
        user_initiated=True,
    )
    parent["agent_session_id"] = "legacy-parent-native-sid"
    child = session_store.fork_session(
        parent,
        parent["id"],
        name="legacy-child",
    )
    legacy_overlay = {
        "id": "legacy-overlay",
        "status": "running",
        "original_text": "preserve exactly",
    }
    child["kind"] = "adv_sync_fork"
    child["user_initiated"] = False
    parent["adv_sync_overlays"] = [legacy_overlay.copy()]
    session_store.write_session_full(parent, bump_updated_at=True)

    loaded_parent = manager.get(parent["id"])
    loaded_child = manager.get(child["id"])
    stored_child = session_store.get_session(child["id"])
    assert loaded_parent is not None
    assert loaded_child is not None
    assert stored_child is not None
    assert loaded_child["kind"] == "adv_sync_fork"
    assert stored_child["kind"] == "adv_sync_fork"
    assert loaded_parent["adv_sync_overlays"] == [legacy_overlay]

    summary = next(
        row for row in session_store.list_sessions()
        if row["id"] == parent["id"]
    )
    assert summary["fork_count"] == 0
    assert summary["all_fork_ids"] == [child["id"]]
    assert manager._is_user_kind(loaded_child) is False

    coordinator._prompt_queues.pop(child["id"], None)
    coordinator._processor_tasks.pop(child["id"], None)
    try:
        manager.admit_queued_prompt_durable(
            child["id"],
            {"id": "legacy-durable", "content": "must not run"},
        )
    except RuntimeError as error:
        assert str(error) == "prompt target belongs to a removed feature"
    else:
        raise AssertionError("legacy durable admission must fail closed")
    try:
        coordinator.submit_prompt(
            child["id"],
            {"_queued_id": "legacy-direct", "prompt": "must not run"},
        )
    except RuntimeError as error:
        assert str(error) == "prompt target belongs to a removed feature"
    else:
        raise AssertionError("legacy direct submission must fail closed")
    assert child["id"] not in coordinator._prompt_queues
    assert child["id"] not in coordinator._processor_tasks
    assert loaded_child.get("queued_prompts") in (None, [])

    parent_item = coordinator.submit_prompt(
        parent["id"],
        {"_queued_id": "parent-accepted", "prompt": "still usable"},
        start_processor=False,
    )
    assert parent_item == "parent-accepted"
    assert coordinator._prompt_queues[parent["id"]].qsize() == 1
    coordinator._prompt_queues.pop(parent["id"], None)
    coordinator._queued_ids.pop(parent["id"], None)

    print("PASS removed routes and startup task are absent")
    print("PASS persisted legacy child loads hidden with overlay intact")
    print("PASS legacy child rejects before queue work; parent remains usable")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
