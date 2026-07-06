#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import sys
import json
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-api-auth-surface-")
os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import FastAPI  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.testclient import TestClient, WebSocketDisconnect  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import node_link  # noqa: E402
import extension_store  # noqa: E402
from _extension_test_helpers import install_machine_nodes_extension  # noqa: E402
from stores import pending_node_registrations  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _sample_path(path: str) -> str:
    return _PARAM_RE.sub(lambda match: f"test-{match.group(1)}", path)


def _is_public(path: str) -> bool:
    # `_is_extension_frontend_asset` matches on concrete paths; sample the
    # route template so a `{extension_id}/frontend/{asset_path:path}` route
    # is recognized as public without enumerating extension ids.
    sampled = _sample_path(path)
    return (
        path in main._AUTH_PUBLIC_ROUTES
        or path in main._AUTH_PUBLIC_ARTIFACT_ROUTES
        or any(path.startswith(prefix) for prefix in main._AUTH_PUBLIC_PREFIXES)
        or main._is_extension_frontend_asset(sampled)
    )


def _api_routes() -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for route in main.app.routes:
        if not isinstance(route, Route):
            continue
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            routes.append((method, path))
    return routes


def _websocket_routes() -> set[str]:
    return {
        getattr(route, "path", "")
        for route in main.app.routes
        if isinstance(route, WebSocketRoute)
    }


_FRONTEND_FIXTURE_ID = "auth-surface-frontend-fixture"
_FRONTEND_FIXTURE_ASSET_BODY = "// extension frontend bundle (public static asset)\n"


def _install_frontend_extension() -> str:
    """Install an enabled extension that ships a real frontend bundle, so a
    handler-level auth regression (not just a middleware-routing change) is
    catchable via an unauthenticated 200 on its asset."""
    package = Path(_TMP_HOME) / "private-fixtures" / _FRONTEND_FIXTURE_ID
    if package.exists():
        shutil.rmtree(package)
    (package / "ui").mkdir(parents=True)
    (package / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    (package / "ui" / "bundle.js").write_text(_FRONTEND_FIXTURE_ASSET_BODY, encoding="utf-8")
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": _FRONTEND_FIXTURE_ID,
        "name": _FRONTEND_FIXTURE_ID,
        "version": "1.0.0",
        "description": _FRONTEND_FIXTURE_ID,
        "surfaces": ["frontend_feature"],
        "entrypoints": {"frontend": "ui/index.html"},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": _FRONTEND_FIXTURE_ID,
        },
        persist=True,
    )
    return _FRONTEND_FIXTURE_ID


def test_public_allowlist_is_exact() -> tuple[bool, str]:
    expected_routes = {
        "/api/auth/login",
        "/api/auth/setup",
        "/api/auth/needs_setup",
    }
    if main._AUTH_PUBLIC_ROUTES != expected_routes:
        return False, f"unexpected public routes: {sorted(main._AUTH_PUBLIC_ROUTES)}"
    expected_artifact_routes = {"/api/desktop/status"}
    if main._AUTH_PUBLIC_ARTIFACT_ROUTES != expected_artifact_routes:
        return False, f"unexpected public artifact routes: {sorted(main._AUTH_PUBLIC_ARTIFACT_ROUTES)}"
    expected_prefixes = ("/api/desktop/updates/", "/api/download/desktop/")
    if main._AUTH_PUBLIC_PREFIXES != expected_prefixes:
        return False, f"unexpected public prefixes: {main._AUTH_PUBLIC_PREFIXES!r}"
    expected_registered_public = {
        ("GET", "/api/auth/needs_setup"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/setup"),
        ("GET", "/api/desktop/status"),
        ("GET", "/api/desktop/updates/{rel_path:path}"),
        ("GET", "/api/download/desktop/macos"),
        ("GET", "/api/download/desktop/windows"),
        # Static UI bundle served public so cross-origin import() on the
        # native shell can load it without a cookie/bearer it cannot send.
        ("GET", "/api/extensions/{extension_id}/frontend/{asset_path:path}"),
    }
    registered_public = {
        (method, path)
        for method, path in _api_routes()
        if _is_public(path)
    }
    if registered_public != expected_registered_public:
        return False, f"unexpected registered public routes: {sorted(registered_public)}"
    return True, ""


def test_websocket_surface_is_exact() -> tuple[bool, str]:
    expected = {"/ws/chat", "/{_unknown_ws_path:path}"}
    actual = _websocket_routes()
    if actual != expected:
        return False, f"unexpected websocket routes: {sorted(actual)}"
    return True, ""


def test_logout_requires_auth() -> tuple[bool, str]:
    res = TestClient(main.app, client=("127.0.0.1", 50000)).post("/api/auth/logout")
    return res.status_code == 401, f"expected 401, got {res.status_code}: {res.text[:120]}"


def test_registered_api_routes_fail_closed_without_auth() -> tuple[bool, str]:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    failures: list[str] = []
    for method, route_path in _api_routes():
        path = _sample_path(route_path)
        res = client.request(method, path)
        if route_path.startswith("/api/internal/"):
            if res.status_code != 403:
                failures.append(f"{method} {route_path} expected internal-token 403, got {res.status_code}")
            continue
        if _is_public(route_path):
            continue
        if res.status_code != 401:
            failures.append(f"{method} {route_path} expected auth 401, got {res.status_code}")
    return not failures, "; ".join(failures[:10])


def test_chat_websocket_requires_auth() -> tuple[bool, str]:
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)).websocket_connect(
            "/ws/chat",
            headers={"Origin": "http://localhost:8000", "Host": "localhost:8000"},
        ) as websocket:
            websocket.receive_text()
            return False, "websocket stayed open without auth"
    except WebSocketDisconnect as exc:
        return exc.code == 1008, f"expected close code 1008, got {exc.code}"


def test_unknown_websocket_fails_closed() -> tuple[bool, str]:
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)).websocket_connect(
            "/api/healthz",
            headers={"Origin": "http://localhost:8000", "Host": "localhost:8000"},
        ) as websocket:
            websocket.receive_text()
            return False, "unknown websocket stayed open"
    except WebSocketDisconnect as exc:
        return exc.code == 1008, f"expected close code 1008, got {exc.code}"


def test_node_websocket_requires_node_auth() -> tuple[bool, str]:
    node_id = "authless-node"
    install_machine_nodes_extension(_TMP_HOME)
    app = FastAPI()
    app.include_router(node_link.router)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(
            "/api/node/connect"
        ) as websocket:
            websocket.send_json({
                "type": "handshake",
                "protocol_version": node_link.PROTOCOL_VERSION,
                "node_id": node_id,
                "registration": {"address": "ws://127.0.0.1:9999", "cwd_roots": ["/tmp"]},
            })
            reject = websocket.receive_json()
            if reject.get("type") != "handshake_reject":
                return False, f"expected handshake_reject, got {reject!r}"
            if pending_node_registrations.get(node_id) is not None:
                return False, "authless node created a pending registration"
            websocket.receive_text()
            return False, "node websocket stayed open without auth"
    except WebSocketDisconnect as exc:
        return exc.code == 1008, f"expected close code 1008, got {exc.code}"


def test_extension_frontend_asset_is_public() -> tuple[bool, str]:
    # The native Capacitor shell loads extension UI bundles via cross-origin
    # import(), which can send neither cookie nor Authorization. The static
    # asset must therefore be served unauthenticated. Assert a REAL installed
    # bundle returns 200 with content — this catches a handler-level auth
    # re-introduction, not just a middleware-routing change.
    extension_id = _install_frontend_extension()
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    res = client.get(f"/api/extensions/{extension_id}/frontend/ui/bundle.js")
    if res.status_code != 200:
        return False, f"expected 200 for public asset, got {res.status_code}: {res.text[:120]}"
    if res.text != _FRONTEND_FIXTURE_ASSET_BODY:
        return False, f"unexpected asset body: {res.text[:120]}"
    # A non-existent asset under a real extension must 404 (not 401) — auth
    # is bypassed, the file simply isn't there.
    missing = client.get(f"/api/extensions/{extension_id}/frontend/ui/missing.js")
    if missing.status_code == 401:
        return False, "missing asset required auth (401) instead of 404"
    return True, ""


def test_extension_backend_route_still_requires_auth() -> tuple[bool, str]:
    # The sibling /backend/ dispatch route must stay auth-gated so making
    # /frontend/ public doesn't expose extension backend logic.
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    res = client.get("/api/extensions/some-ext/backend/anything")
    return res.status_code == 401, f"expected backend route 401, got {res.status_code}"


def test_extension_entrypoints_route_still_requires_auth() -> tuple[bool, str]:
    # The route that enumerates installed extensions is fetched via
    # window.fetch (bearer-attached), so it must stay auth-gated even though
    # /frontend/ assets are now public.
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    res = client.get("/api/extensions/frontend-entrypoints")
    return res.status_code == 401, f"expected entrypoints 401, got {res.status_code}"


def test_frontend_log_drops_info_noise() -> tuple[bool, str]:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    headers = {"Authorization": f"Bearer {auth.create_token('test')}"}
    res = client.post(
        "/api/logs/frontend",
        json={
            "level": "info",
            "source": "console",
            "message": "TESTAPE_SDK custom_state noisy",
        },
        headers=headers,
    )
    if res.status_code != 200:
        return False, f"expected 200, got {res.status_code}: {res.text[:120]}"
    body = res.json()
    return body.get("dropped") is True, f"expected dropped response, got {body!r}"


TESTS = [
    ("public API allowlist is exact", test_public_allowlist_is_exact),
    ("websocket surface is exact", test_websocket_surface_is_exact),
    ("logout requires auth", test_logout_requires_auth),
    ("registered API routes fail closed without auth", test_registered_api_routes_fail_closed_without_auth),
    ("chat websocket requires auth", test_chat_websocket_requires_auth),
    ("unknown websocket fails closed", test_unknown_websocket_fails_closed),
    ("node websocket requires node auth", test_node_websocket_requires_node_auth),
    ("extension frontend asset is public", test_extension_frontend_asset_is_public),
    ("extension backend route still requires auth", test_extension_backend_route_still_requires_auth),
    ("extension entrypoints route still requires auth", test_extension_entrypoints_route_still_requires_auth),
    ("frontend log drops info noise", test_frontend_log_drops_info_noise),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"exception: {exc}"
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' - ' + detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
