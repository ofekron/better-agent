from __future__ import annotations

import json
import os
import re
import base64
import hashlib
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import password_manager

_MARKETPLACE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BetterAgent/marketplace-extension",
}

# Extension ids are namespace.name over [A-Za-z0-9._-]. Anything else is rejected
# before being interpolated into a hosted URL path so a crafted id cannot inject
# path segments ("../") or query fragments ("?", "#") into the marketplace request.
_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_AUTH_SERVICE = "better-agent-marketplace"
_SESSION_ACCOUNT = "oauth-session"
_PENDING_PREFIX = "oauth-pending-"
_AUTH_TTL_SECONDS = 600
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{20,160}$")
_refresh_lock = threading.Lock()


def _marketplace_base_url() -> str:
    value = str(os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL") or "https://singular-labs.ai/api/marketplace").strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)):
        raise RuntimeError("marketplace base URL must use HTTPS")
    return value


def _require_extension_id(extension_id: str) -> str:
    clean = str(extension_id or "").strip()
    if not _EXTENSION_ID_RE.fullmatch(clean):
        raise HTTPException(status_code=400, detail="invalid extension id")
    return clean


def _provider_login_url(provider: str) -> str:
    if provider not in {"google", "github"}:
        raise HTTPException(status_code=404, detail="unknown login provider")
    return f"{_marketplace_base_url()}/auth/login/{provider}"


def _json_request(path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{_marketplace_base_url()}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={**_MARKETPLACE_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(status_code=401, detail="marketplace login required") from exc
        raise HTTPException(status_code=502, detail="marketplace authentication is unavailable") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="marketplace authentication is unavailable") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="marketplace authentication is invalid")
    return result


def _store_secret(account: str, value: dict[str, object]) -> None:
    password_manager.store_service_password({
        "service": _AUTH_SERVICE,
        "account": account,
        "password": json.dumps(value, separators=(",", ":")),
    })


def _load_secret(account: str) -> dict[str, object] | None:
    try:
        value = json.loads(password_manager.get_service_password(_AUTH_SERVICE, account))
    except (password_manager.PasswordManagerError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _delete_secret(account: str) -> None:
    try:
        password_manager.delete_service_password({"service": _AUTH_SERVICE, "account": account})
    except password_manager.PasswordManagerError:
        pass


def _cleanup_pending() -> None:
    now = int(time.time())
    for item in password_manager.list_service_passwords().get("items", []):
        if item.get("service") != _AUTH_SERVICE:
            continue
        account = str(item.get("account") or "")
        if not account.startswith(_PENDING_PREFIX):
            continue
        pending = _load_secret(account)
        if not pending or int(pending.get("expires_at") or 0) <= now:
            _delete_secret(account)


def _session_tokens() -> dict[str, object] | None:
    return _load_secret(_SESSION_ACCOUNT)


def _expires_at_epoch(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _refresh_session(tokens: dict[str, object]) -> dict[str, object]:
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="marketplace login required")
    with _refresh_lock:
        current = _session_tokens()
        if current and current != tokens and _expires_at_epoch(current.get("access_token_expires_at")) > int(time.time()) + 30:
            return current
        try:
            rotated = _json_request("/auth/app/refresh", {"refresh_token": refresh_token})
        except HTTPException as exc:
            if exc.status_code == 401:
                _delete_secret(_SESSION_ACCOUNT)
            raise
        rotated["provider"] = str(tokens.get("provider") or "")
        _store_secret(_SESSION_ACCOUNT, rotated)
        return rotated


def _access_token() -> str:
    tokens = _session_tokens()
    if not tokens:
        return ""
    if _expires_at_epoch(tokens.get("access_token_expires_at")) <= int(time.time()) + 30:
        tokens = _refresh_session(tokens)
    return str(tokens.get("access_token") or "")


def _marketplace_headers(access_token: str) -> dict[str, str]:
    headers = dict(_MARKETPLACE_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _ofekdev_rows(access_token: str) -> list[dict[str, object]]:
    # The hosted marketplace is a static publish: the aggregate catalog lives at
    # extensions.json (not /extensions, which is the artifact directory and would
    # return a filesystem autoindex). Per-item metadata lives at extensions/<id>/metadata.
    url = f"{_marketplace_base_url()}/extensions.json"
    request = urllib.request.Request(url, headers=_marketplace_headers(access_token))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(status_code=401, detail="marketplace login required") from exc
        raise HTTPException(status_code=502, detail="marketplace catalog is unavailable") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="marketplace catalog is unavailable") from exc
    rows = []
    for item in payload.get("extensions") or []:
        if not isinstance(item, dict):
            continue
        extension_id = _require_extension_id(str(item.get("id") or ""))
        rows.append(
            {
                "id": extension_id,
                "name": str(item.get("name") or ""),
                "version": str(item.get("version") or ""),
                "description": str(item.get("description") or ""),
                "surfaces": list(item.get("surfaces") or []),
                "marketplace": dict(item.get("marketplace") or {}),
                "install": {
                    "metadata_url": f"/api/extensions/ofek-dev.marketplace/backend/metadata/{extension_id}",
                },
            }
        )
    return rows


def _ofekdev_metadata(extension_id: str, access_token: str) -> dict[str, object]:
    safe_id = _require_extension_id(extension_id)
    url = f"{_marketplace_base_url()}/extensions/{safe_id}/metadata"
    request = urllib.request.Request(url, headers=_marketplace_headers(access_token))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(status_code=401, detail="marketplace login required") from exc
        raise HTTPException(status_code=502, detail="marketplace metadata is unavailable") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="marketplace metadata is unavailable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="marketplace metadata is invalid")
    return payload


def _ofekdev_uninstall(extension_id: str, access_token: str) -> None:
    safe_id = _require_extension_id(extension_id)
    url = f"{_marketplace_base_url()}/extensions/{safe_id}/uninstall"
    request = urllib.request.Request(
        url,
        headers=_marketplace_headers(access_token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(status_code=401, detail="marketplace login required") from exc
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="extension not found") from exc
        raise HTTPException(status_code=502, detail="marketplace uninstall is unavailable") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="marketplace uninstall is unavailable") from exc


def create_router(context) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/auth/providers")
    async def auth_providers() -> dict[str, object]:
        return {
            "providers": [
                {"id": "google", "label": "Google"},
                {"id": "github", "label": "GitHub"},
            ]
        }

    @router.post("/auth/start")
    async def auth_start(request: Request) -> dict[str, str]:
        body = await request.json()
        provider = str(body.get("provider") or "") if isinstance(body, dict) else ""
        _provider_login_url(provider)
        app_session = request.cookies.get("better_agent_session", "")
        if not app_session:
            raise HTTPException(status_code=401, detail="Better Agent login required")
        _cleanup_pending()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        callback = str(request.base_url).rstrip("/") + "/api/extensions/ofek-dev.marketplace/backend/auth/callback"
        started = _json_request("/auth/app/start", {
            "provider": provider,
            "app_redirect": callback,
            "code_challenge": challenge,
        })
        state = str(started.get("state") or "")
        login_url = str(started.get("login_url") or "")
        if not _STATE_RE.fullmatch(state) or not login_url:
            raise HTTPException(status_code=502, detail="marketplace authentication is invalid")
        _store_secret(_PENDING_PREFIX + state, {
            "provider": provider,
            "verifier": verifier,
            "app_session_digest": hashlib.sha256(app_session.encode("utf-8")).hexdigest(),
            "expires_at": int(time.time()) + _AUTH_TTL_SECONDS,
        })
        return {"login_url": login_url, "state": state}

    @router.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = "") -> HTMLResponse:
        if not _STATE_RE.fullmatch(state):
            raise HTTPException(status_code=401, detail="marketplace authentication expired")
        pending_account = _PENDING_PREFIX + state
        pending = _load_secret(pending_account)
        _delete_secret(pending_account)
        app_session_digest = hashlib.sha256(request.cookies.get("better_agent_session", "").encode("utf-8")).hexdigest()
        if (
            not code
            or not pending
            or int(pending.get("expires_at") or 0) <= int(time.time())
            or not secrets.compare_digest(str(pending.get("app_session_digest") or ""), app_session_digest)
        ):
            raise HTTPException(status_code=401, detail="marketplace authentication expired")
        tokens = _json_request("/auth/app/exchange", {
            "code": code,
            "code_verifier": str(pending.get("verifier") or ""),
        })
        tokens["provider"] = str(pending.get("provider") or "")
        _store_secret(_SESSION_ACCOUNT, tokens)
        state_json = json.dumps(state)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Marketplace sign-in complete</title>"
            "<p>Sign-in complete. You can close this window.</p>"
            f"<script>if(window.opener)window.opener.postMessage({{source:'better-agent-marketplace-auth',state:{state_json}}},'*');window.close();</script>"
        )

    @router.get("/auth/status")
    async def auth_status() -> dict[str, object]:
        tokens = _session_tokens()
        return {"authenticated": bool(tokens), "provider": str((tokens or {}).get("provider") or "")}

    @router.post("/auth/logout")
    async def auth_logout() -> dict[str, bool]:
        tokens = _session_tokens()
        if tokens and tokens.get("refresh_token"):
            try:
                _json_request("/auth/app/logout", {"refresh_token": str(tokens["refresh_token"])})
            finally:
                _delete_secret(_SESSION_ACCOUNT)
        else:
            _delete_secret(_SESSION_ACCOUNT)
        return {"ok": True}

    @router.get("/catalog")
    async def catalog() -> dict[str, object]:
        return {"extensions": _ofekdev_rows(_access_token())}

    @router.get("/metadata/{extension_id}")
    async def metadata(extension_id: str) -> dict[str, object]:
        return _ofekdev_metadata(extension_id, _access_token())

    @router.post("/extensions/{extension_id}/uninstall")
    async def extension_uninstall(extension_id: str) -> dict[str, bool]:
        _ofekdev_uninstall(extension_id, _access_token())
        return {"ok": True}

    return router
