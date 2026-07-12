from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_HOME = _test_home.isolate("bc-test-assistant-destination-")

import extension_store  # noqa: E402
import extension_backend_loader  # noqa: E402
import lag_incident_queue  # noqa: E402


def _record(
    root: Path,
    *,
    generation: str,
    enabled: bool,
    extension_id: str = extension_store.ASSISTANT_EXTENSION_ID,
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    return {
        "manifest": {
            "id": extension_id,
            "name": "Assistant",
            "version": "1.0.0",
            "core_roles": ["assistant"],
            "entrypoints": {"backend": "backend/routes.py"},
            "permissions": {"backend_routes": True},
        },
        "enabled": enabled,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test",
            "commit_sha": generation,
            "install_path": str(root),
        },
        "entitlement": {"status": "active"},
    }


def main() -> None:
    calls = 0
    original_sync = lag_incident_queue.synchronize_destination

    def notified(_identity: str) -> bool:
        nonlocal calls
        calls += 1
        return True

    lag_incident_queue.synchronize_destination = notified
    try:
        data = extension_store._load()
        data["extensions"].pop(extension_store.ASSISTANT_EXTENSION_ID, None)
        data["extensions"].pop("aaa.spoof-assistant", None)
        extension_store._save(
            data,
            deleted_extension_ids={extension_store.ASSISTANT_EXTENSION_ID, "aaa.spoof-assistant"},
        )
        calls = 0

        install_root = Path(tempfile.mkdtemp(prefix="assistant-package-"))
        data = extension_store._load()
        data["extensions"]["aaa.spoof-assistant"] = _record(
            install_root / "spoof",
            generation="spoof-generation-a",
            enabled=False,
            extension_id="aaa.spoof-assistant",
        )
        extension_store._save(data, resurrect_extension_ids={"aaa.spoof-assistant"})
        assert calls == 0, "spoof-only Assistant role claimant must remain absent"

        data = extension_store._load()
        data["extensions"][extension_store.ASSISTANT_EXTENSION_ID] = _record(
            install_root, generation="generation-a", enabled=True,
        )
        extension_store._save(
            data, resurrect_extension_ids={extension_store.ASSISTANT_EXTENSION_ID},
        )
        assert calls == 1

        spoof_replaced = copy.deepcopy(extension_store._load())
        spoof_replaced["extensions"]["aaa.spoof-assistant"]["source"]["commit_sha"] = (
            "spoof-generation-b"
        )
        extension_store._save(spoof_replaced)
        assert calls == 1, "spoof generation cannot affect trusted destination identity"

        unchanged = copy.deepcopy(extension_store._load())
        unchanged["extensions"]["ofek-dev.assistant"]["updated_at"] = "metadata-only"
        extension_store._save(unchanged)
        assert calls == 1, "metadata-only writes must not wake the destination"

        disabled = copy.deepcopy(extension_store._load())
        disabled["extensions"]["ofek-dev.assistant"]["enabled"] = False
        extension_store._save(disabled)
        assert calls == 2

        replacement = copy.deepcopy(extension_store._load())
        replacement["extensions"]["ofek-dev.assistant"]["source"]["commit_sha"] = "generation-b"
        extension_store._save(replacement)
        assert calls == 3, "replacement generation must wake even while unavailable"

        extension_store._save(copy.deepcopy(extension_store._load()))
        assert calls == 3, "same state after process restart must not create a generation"

        enabled = copy.deepcopy(extension_store._load())
        enabled["extensions"]["ofek-dev.assistant"]["enabled"] = True
        extension_store._save(enabled)
        assert calls == 4

        quarantined = copy.deepcopy(extension_store._load())
        quarantined["extensions"]["ofek-dev.assistant"]["enabled"] = False
        quarantined["extensions"]["ofek-dev.assistant"]["quarantine"] = {"reason": "test"}
        extension_store._save(quarantined)
        assert calls == 5

        absent = copy.deepcopy(extension_store._load())
        absent["extensions"].pop("ofek-dev.assistant")
        extension_store._save(absent, deleted_extension_ids={"ofek-dev.assistant"})
        assert calls == 6
        outcome = extension_backend_loader.dispatch_named_core_destination_sync(
            "assistant.lag-report", body_bytes=b"{}",
        )
        assert outcome.availability is extension_backend_loader.DestinationAvailability.ABSENT
        assert outcome.status == 404
    finally:
        lag_incident_queue.synchronize_destination = original_sync
        shutil.rmtree(_HOME, ignore_errors=True)
    print("PASS: authoritative Assistant destination generation")


def test_startup_repairs_store_replace_before_notification() -> None:
    shutil.rmtree(Path(_HOME) / "lag-incidents", ignore_errors=True)
    extension_store.synchronize_assistant_destination()
    before = lag_incident_queue._destination_state()[0]
    install_root = Path(tempfile.mkdtemp(prefix="assistant-crash-package-"))
    original_sync = lag_incident_queue.synchronize_destination

    def crash_after_store_replace(_identity: str) -> bool:
        raise SystemExit(73)

    lag_incident_queue.synchronize_destination = crash_after_store_replace
    try:
        data = extension_store._load()
        data["extensions"]["ofek-dev.assistant"] = _record(
            install_root, generation="post-crash-generation", enabled=True,
        )
        try:
            extension_store._save(data, resurrect_extension_ids={"ofek-dev.assistant"})
        except SystemExit as exc:
            assert exc.code == 73
        else:
            raise AssertionError("crash cutpoint did not fire")
    finally:
        lag_incident_queue.synchronize_destination = original_sync

    assert extension_store.get_extension("ofek-dev.assistant") is not None
    assert lag_incident_queue._destination_state()[0] == before
    assert extension_store.synchronize_assistant_destination()
    repaired = lag_incident_queue._destination_state()[0]
    assert repaired == before + 1
    assert not extension_store.synchronize_assistant_destination()
    assert lag_incident_queue._destination_state()[0] == repaired


if __name__ == "__main__":
    main()
    test_startup_repairs_store_replace_before_notification()
