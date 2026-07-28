from __future__ import annotations

import atexit
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _test_home

_TEST_HOME = _test_home.TestHome.acquire("extension-health-decisions-")
atexit.register(_TEST_HOME.release)

import extension_store
import extension_token_registry


def _record(extension_id: str, dependencies: list[str] | None = None) -> dict:
    return {
        "manifest": {
            "id": extension_id,
            "dependencies": dependencies or [],
            "permissions": {"internal_loopback": True},
        },
        "enabled": True,
        "activation_id": f"{extension_id}-activation",
        "source": {"type": "test"},
        "entitlement": {"status": "not_required"},
    }


def _seed() -> None:
    extension_store._save(
        {
            "schema_version": extension_store.STORE_SCHEMA_VERSION,
            "extensions": {
                "probe.base": _record("probe.base"),
                "probe.dependent": _record("probe.dependent", ["probe.base"]),
            },
            "deleted_extensions": {},
        }
    )
    extension_store._clear_slow_call_history("probe.base")


def _trigger() -> list[str]:
    result: list[str] = []
    for elapsed in (3.0, 3.1, 3.2):
        result = extension_store.record_slow_backend_call(
            "probe.base",
            activation_id="probe.base-activation",
            elapsed_seconds=elapsed,
            path="work",
        )
    return result


def test_threshold_requests_decision_without_disabling() -> None:
    _seed()
    if _trigger() != ["probe.base", "probe.dependent"]:
        raise AssertionError("expected exact dependent cohort")
    base = extension_store.get_extension("probe.base") or {}
    dependent = extension_store.get_extension("probe.dependent") or {}
    if base.get("enabled") is not True or dependent.get("enabled") is not True:
        raise AssertionError("incident automatically disabled an extension")
    decision = base.get("pending_health_decision") or {}
    if [item["extension_id"] for item in decision.get("cohort", [])] != [
        "probe.base",
        "probe.dependent",
    ]:
        raise AssertionError(decision)
    if _trigger():
        raise AssertionError("duplicate pending decision was created")


def test_keep_enabled_and_disable_are_explicit() -> None:
    _seed()
    _trigger()
    decision = (extension_store.get_extension("probe.base") or {})[
        "pending_health_decision"
    ]
    extension_store.resolve_health_decision(
        "probe.base", decision_id=decision["id"], action="keep_enabled"
    )
    if not all(
        (extension_store.get_extension(item) or {}).get("enabled") is True
        for item in ("probe.base", "probe.dependent")
    ):
        raise AssertionError("keep enabled changed activation")

    _trigger()
    decision = (extension_store.get_extension("probe.base") or {})[
        "pending_health_decision"
    ]
    extension_token_registry.mint("probe.base")
    extension_token_registry.mint("probe.dependent")
    extension_store.resolve_health_decision(
        "probe.base", decision_id=decision["id"], action="disable"
    )
    if any(
        (extension_store.get_extension(item) or {}).get("enabled") is True
        for item in ("probe.base", "probe.dependent")
    ):
        raise AssertionError("approved cohort was not disabled")
    if {"probe.base", "probe.dependent"} & extension_token_registry.extension_ids():
        raise AssertionError("authoritative token reconciliation retained disabled owners")


def test_stale_cohort_is_rejected_and_manual_change_invalidates() -> None:
    _seed()
    _trigger()
    decision = (extension_store.get_extension("probe.base") or {})[
        "pending_health_decision"
    ]
    data = extension_store._load()
    data["extensions"]["probe.new"] = _record("probe.new", ["probe.base"])
    extension_store._save(data)
    try:
        extension_store.resolve_health_decision(
            "probe.base", decision_id=decision["id"], action="disable"
        )
    except extension_store.ExtensionError as exc:
        if "cohort changed" not in str(exc):
            raise
    else:
        raise AssertionError("stale cohort decision applied")

    data = extension_store._load()
    data["extensions"].pop("probe.new")
    extension_store._save(data)
    extension_store.set_enabled("probe.dependent", False)
    if (extension_store.get_extension("probe.base") or {}).get(
        "pending_health_decision"
    ):
        raise AssertionError("manual cohort mutation left a stale decision")


def main() -> None:
    test_threshold_requests_decision_without_disabling()
    test_keep_enabled_and_disable_are_explicit()
    test_stale_cohort_is_rejected_and_manual_change_invalidates()
    print("extension health decision tests passed")


if __name__ == "__main__":
    main()
