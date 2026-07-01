import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_tmp = _test_home.isolate("ba-consent-cache-")

from credential_broker import consent_store


def _consents_dir() -> Path:
    path = Path(_tmp) / "credential_broker" / "consents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(consent_id: str, *, status: str = "pending", app_session_id: str = "app-1") -> dict:
    return {
        "consent_id": consent_id,
        "app_session_id": app_session_id,
        "provider_id": "provider-1",
        "secret_ref": None,
        "secret_refs": None,
        "descriptor": {
            "label": consent_id,
            "secret_names": ["secret"],
            "secret_sources": {"secret": {"kind": "manual", "service": "svc"}},
        },
        "descriptor_hash": "hash",
        "sink": {"kind": "http", "url": f"https://example.test/{consent_id}"},
        "status": status,
        "created_at": f"2026-01-01T00:00:{consent_id[-1] if consent_id[-1].isdigit() else '0'}",
        "expires_at": "2099-01-01T00:00:00",
        "resolved_at": None,
        "use_count": 0,
        "last_used_at": None,
    }


def _write_record(consent_id: str, *, status: str = "pending", app_session_id: str = "app-1") -> None:
    (_consents_dir() / f"{consent_id}.json").write_text(
        json.dumps(_record(consent_id, status=status, app_session_id=app_session_id), indent=2),
        encoding="utf-8",
    )


def _reset() -> None:
    for path in _consents_dir().glob("*.json"):
        path.unlink()
    consent_store._reset_cache_for_tests()


def test_pending_listing_scans_once_and_filters_from_cache() -> None:
    _reset()
    _write_record("pending-1", app_session_id="app-1")
    _write_record("pending-2", app_session_id="app-2")
    _write_record("approved-1", status="approved", app_session_id="app-1")
    scans = 0
    original = consent_store._iter_consent_paths

    def counted():
        nonlocal scans
        scans += 1
        return original()

    consent_store._iter_consent_paths = counted
    try:
        assert [rec["consent_id"] for rec in consent_store.list_pending()] == ["pending-1", "pending-2"]
        assert scans == 1
        assert [rec["consent_id"] for rec in consent_store.list_pending(app_session_id="app-1")] == ["pending-1"]
        assert consent_store.list_pending(app_session_id="missing") == []
        assert scans == 1
    finally:
        consent_store._iter_consent_paths = original


def test_pending_listing_returns_deep_copies() -> None:
    _reset()
    _write_record("pending-1")
    listed = consent_store.list_pending()
    listed[0]["descriptor"]["label"] = "mutated"
    listed[0]["sink"]["url"] = "mutated"
    again = consent_store.list_pending()[0]
    assert again["descriptor"]["label"] == "pending-1"
    assert again["sink"]["url"] == "https://example.test/pending-1"


def test_store_writes_invalidate_pending_cache() -> None:
    _reset()
    assert consent_store.list_pending() == []
    created = consent_store.create(
        consent_id="pending-1",
        app_session_id="app-1",
        provider_id="provider-1",
        descriptor=_record("pending-1")["descriptor"],
        descriptor_hash="hash",
        sink_public={"kind": "http"},
    )
    assert [rec["consent_id"] for rec in consent_store.list_pending()] == [created["consent_id"]]
    rec, reason = consent_store.deny("pending-1")
    assert reason == "ok", rec
    assert consent_store.list_pending() == []

    consent_store.create(
        consent_id="pending-2",
        app_session_id="app-1",
        provider_id="provider-1",
        descriptor=_record("pending-2")["descriptor"],
        descriptor_hash="hash",
        sink_public={"kind": "http"},
    )
    rec, reason = consent_store.approve("pending-2", secret_ref="ref")
    assert reason == "ok", rec
    assert consent_store.list_pending() == []

    consent_store.create(
        consent_id="pending-3",
        app_session_id="app-1",
        provider_id="provider-1",
        descriptor=_record("pending-3")["descriptor"],
        descriptor_hash="hash",
        sink_public={"kind": "http"},
    )
    rec, reason = consent_store.revoke("pending-3")
    assert reason == "ok", rec
    assert consent_store.list_pending() == []

    consent_store.create(
        consent_id="pending-4",
        app_session_id="app-1",
        provider_id="provider-1",
        descriptor=_record("pending-4")["descriptor"],
        descriptor_hash="hash",
        sink_public={"kind": "http"},
    )
    assert consent_store.delete("pending-4") is True
    assert consent_store.list_pending() == []


def test_prune_and_malformed_handling() -> None:
    _reset()
    _write_record("pending-1")
    (_consents_dir() / "bad.json").write_text("{", encoding="utf-8")
    assert [rec["consent_id"] for rec in consent_store.list_pending()] == ["pending-1"]
    old_path = _consents_dir() / "pending-1.json"
    os.utime(old_path, (1, 1))
    assert consent_store.prune_old(max_age_days=1) == 1
    assert consent_store.list_pending() == []


def test_direct_file_paths_bypass_stale_pending_cache() -> None:
    _reset()
    _write_record("pending-1")
    assert consent_store.list_pending()[0]["sink"]["url"] == "https://example.test/pending-1"
    edited = _record("pending-1")
    edited["sink"]["url"] = "https://example.test/direct-edit"
    (_consents_dir() / "pending-1.json").write_text(json.dumps(edited), encoding="utf-8")
    assert consent_store.list_pending()[0]["sink"]["url"] == "https://example.test/pending-1"
    assert consent_store.get("pending-1")["sink"]["url"] == "https://example.test/direct-edit"

    approved = _record("approved-1", status="approved")
    approved["secret_ref"] = "ref"
    (_consents_dir() / "approved-1.json").write_text(json.dumps(approved), encoding="utf-8")
    rec, reason = consent_store.acquire_for_execute("approved-1")
    assert reason == "ok", rec
    assert rec["use_count"] == 1


if __name__ == "__main__":
    test_pending_listing_scans_once_and_filters_from_cache()
    print("PASS: pending listing scans once and filters from cache")
    test_pending_listing_returns_deep_copies()
    print("PASS: pending listing returns deep copies")
    test_store_writes_invalidate_pending_cache()
    print("PASS: store writes invalidate pending cache")
    test_prune_and_malformed_handling()
    print("PASS: prune and malformed handling")
    test_direct_file_paths_bypass_stale_pending_cache()
    print("PASS: direct file paths bypass stale pending cache")
