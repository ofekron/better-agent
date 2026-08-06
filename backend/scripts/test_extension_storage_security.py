#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extension-storage-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import extension_store  # noqa: E402
import extension_storage_api  # noqa: E402
import orchestrator  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
EXT_ID = "ofek.storage"
INTERNAL_TOKEN = "storage-test-token"


class _Coordinator:
    def verify_internal_token(self, token: str) -> bool:
        return token == INTERNAL_TOKEN

    def is_internal_caller(self, token: str) -> bool:
        return token == INTERNAL_TOKEN

    def principal_extension_id(self, token: str):
        # Token IS the identity: this test's token maps to EXT_ID.
        return EXT_ID if token == INTERNAL_TOKEN else None


def _seed_extension() -> dict:
    record = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": EXT_ID,
            "name": "Storage",
            "version": "1.0.0",
            "description": "",
            "surfaces": [],
            "entrypoints": {"mcp": [], "provider_capabilities": []},
            "permissions": {"storage": True},
            "marketplace": {"product_id": "", "subscription_required": False, "entitlement_url": ""},
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {"type": "test", "repo_url": "", "extension_path": "", "ref": "", "commit_sha": "abc", "install_path": ""},
        "entitlement": {"status": "not_required", "product_id": "", "token_present": False, "last_checked_at": "", "expires_at": ""},
    }
    return record


def _apply_test_facade() -> None:
    """Install the in-memory extension record + test coordinator these tests rely on.

    Under pytest this runs via the autouse fixture (which also restores originals);
    under __main__ it runs once at the start of main_run().
    """
    record = _seed_extension()
    orchestrator._default_coordinator = _Coordinator()  # type: ignore[attr-defined]
    extension_store.get_extension = lambda extension_id: record if extension_id == EXT_ID else None  # type: ignore[method-assign]
    extension_store.is_extension_active = lambda extension_id: extension_id == EXT_ID  # type: ignore[method-assign]
    extension_store.has_permission = lambda extension_record, permission: permission == "storage"  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _storage_test_facade():
    saved = {
        "get_extension": extension_store.get_extension,
        "is_extension_active": extension_store.is_extension_active,
        "has_permission": extension_store.has_permission,
        "_default_coordinator": getattr(orchestrator, "_default_coordinator", None),
    }
    _apply_test_facade()
    try:
        yield
    finally:
        extension_store.get_extension = saved["get_extension"]  # type: ignore[method-assign]
        extension_store.is_extension_active = saved["is_extension_active"]  # type: ignore[method-assign]
        extension_store.has_permission = saved["has_permission"]  # type: ignore[method-assign]
        orchestrator._default_coordinator = saved["_default_coordinator"]  # type: ignore[attr-defined]


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(extension_storage_api.router)
    return TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:8000")


def _headers() -> dict[str, str]:
    # Identity is derived from the token alone (no X-Extension-Id header).
    return {"X-Internal-Token": INTERNAL_TOKEN}


def test_put_get_round_trip() -> None:
    client = _client()
    res = client.post(
        "/api/internal/extension-storage/put",
        headers=_headers(),
        json={"key": "state/value.bin", "value_base64": "b2s="},
    )
    assert res.status_code == 200, f"put got {res.status_code}: {res.text[:120]}"
    res = client.post("/api/internal/extension-storage/get", headers=_headers(), json={"key": "state/value.bin"})
    assert res.status_code == 200, f"get got {res.status_code}: {res.text[:120]}"
    assert res.json().get("value_base64") == "b2s=", f"round-trip mismatch: {res.text[:120]}"


def test_leaf_symlink_rejected() -> None:
    root = ba_home() / "extensions" / "storage" / EXT_ID
    root.mkdir(parents=True, exist_ok=True)
    outside = Path(_TMP_HOME) / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "leaf").symlink_to(outside)
    res = _client().post(
        "/api/internal/extension-storage/put",
        headers=_headers(),
        json={"key": "leaf", "value_base64": "bm8="},
    )
    assert res.status_code == 400, f"got {res.status_code}: {res.text[:120]}"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_parent_symlink_rejected() -> None:
    root = ba_home() / "extensions" / "storage" / EXT_ID
    target = Path(_TMP_HOME) / "outside-dir"
    target.mkdir()
    link = root / "linked"
    link.symlink_to(target, target_is_directory=True)
    res = _client().post(
        "/api/internal/extension-storage/put",
        headers=_headers(),
        json={"key": "linked/value", "value_base64": "bm8="},
    )
    assert res.status_code == 400, f"got {res.status_code}: {res.text[:120]}"
    assert not (target / "value").exists()


TESTS = [
    ("storage put/get round trip", test_put_get_round_trip),
    ("storage leaf symlink rejected", test_leaf_symlink_rejected),
    ("storage parent symlink rejected", test_parent_symlink_rejected),
]


def main_run() -> int:
    _apply_test_facade()
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {FAIL} {name} - exception: {exc}")
            failed += 1
            continue
        print(f"  {PASS} {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
