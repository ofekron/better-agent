"""Security regression: extension iframe assets must not authenticate via URL tokens."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extension-iframe-auth-")
os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import extension_store  # noqa: E402
import main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_TOKEN = auth.create_token("native-iframe-user")


def _seed_extension() -> None:
    package = Path(_TMP_HOME) / "fixture-extension"
    (package / "ui").mkdir(parents=True)
    (package / "ui" / "index.html").write_text("<!doctype html><title>ok</title>\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek.iframe"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek.iframe",
            "name": "Iframe",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "frontend_modules": [
                    {
                        "slot": "settings",
                        "id": "iframe",
                        "label": "Iframe",
                        "kind": "iframe",
                        "module": "ui/index.html",
                    }
                ],
                "mcp": [],
                "provider_capabilities": [],
            },
            "permissions": {},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "artifact",
            "repo_url": "https://example.test/ofek.iframe/artifact",
            "extension_path": "",
            "ref": "",
            "commit_sha": "abc",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def _client() -> TestClient:
    return TestClient(main.app, client=("127.0.0.1", 50000))


def test_frontend_asset_query_token_rejected() -> tuple[bool, str]:
    res = _client().get(f"/api/extensions/ofek.iframe/frontend/ui/index.html?token={_TOKEN}")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text}"


def test_other_api_query_token_rejected() -> tuple[bool, str]:
    res = _client().get(f"/api/extensions?token={_TOKEN}")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text}"


def test_bogus_frontend_query_token_rejected() -> tuple[bool, str]:
    res = _client().get("/api/extensions/ofek.iframe/frontend/ui/index.html?token=not-real")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text}"


TESTS = [
    ("extension frontend asset rejects valid query token", test_frontend_asset_query_token_rejected),
    ("non-frontend API rejects query token", test_other_api_query_token_rejected),
    ("extension frontend asset rejects bogus query token", test_bogus_frontend_query_token_rejected),
]


def main_run() -> int:
    _seed_extension()
    failed = 0
    for name, fn in TESTS:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {e}"
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' - ' + detail}")
        if not ok:
            failed += 1
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
