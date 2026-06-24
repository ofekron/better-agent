"""Security invariant: HTTP APIs never authenticate via URL query tokens.

Extension frontend assets (HTML/JS/CSS loaded as browser dynamic imports) are
intentionally public static assets — exactly like the SPA shell — because a
browser module import cannot attach auth headers. They are served without auth,
so a `?token=` query param is irrelevant there (it is neither required nor
honored). Protected APIs, by contrast, MUST reject query-param tokens: the HTTP
auth gate reads only headers/cookies, never the query string (only the WebSocket
path accepts a query token). This test locks that boundary so a future change
cannot accidentally start honoring URL tokens on HTTP."""

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
_FRONTEND_ASSET = "/api/extensions/ofek.iframe/frontend/ui/index.html"


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


def test_frontend_asset_is_public_without_token() -> tuple[bool, str]:
    # Public static asset: served with no credentials at all. This is the
    # security-relevant baseline — the asset is intentionally public, NOT
    # authenticated via any token.
    res = _client().get(_FRONTEND_ASSET)
    return res.status_code == 200, f"expected 200 (public asset), got {res.status_code}: {res.text}"


def test_frontend_asset_ignores_query_token() -> tuple[bool, str]:
    # A query token on the public asset changes nothing — it is not honored as
    # auth. The asset serves exactly as it does with no token.
    res = _client().get(f"{_FRONTEND_ASSET}?token={_TOKEN}")
    return res.status_code == 200, f"expected 200 (token ignored on public asset), got {res.status_code}: {res.text}"


def test_other_api_query_token_rejected() -> tuple[bool, str]:
    # Protected APIs MUST reject query-param tokens — HTTP auth never reads the
    # query string. This is the core guard against URL-token authentication.
    res = _client().get(f"/api/extensions?token={_TOKEN}")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text}"


def test_bogus_query_token_rejected_on_protected_api() -> tuple[bool, str]:
    res = _client().get("/api/extensions?token=not-real")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text}"


TESTS = [
    ("extension frontend asset is public without any token", test_frontend_asset_is_public_without_token),
    ("extension frontend asset ignores query token (not honored as auth)", test_frontend_asset_ignores_query_token),
    ("non-frontend protected API rejects query token", test_other_api_query_token_rejected),
    ("non-frontend protected API rejects bogus query token", test_bogus_query_token_rejected_on_protected_api),
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
