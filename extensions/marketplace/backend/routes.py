from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from better_agent_sdk import BetterAgentError, Client
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

_MARKETPLACE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BetterAgent/marketplace-extension",
}

_SESSION_ACCOUNT = "oauth-session"
_PENDING_PREFIX = "oauth-pending-"
_PENDING_TTL_SECONDS = 600
_ACCESS_REFRESH_SLACK_SECONDS = 60
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_PROTOCOL_VERSION = 1
_PROTOCOL_HASH = "b5c42f92070f9191f8890d2b6e70d159daa71f1558b2e98cdb63282620d10591"
_refresh_lock = threading.RLock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _auth_secret(action: str, payload: dict | None = None) -> dict:
    try:
        return Client().invoke_capability(
            "marketplace",
            f"auth-secret.{action}",
            payload or {},
        )
    except BetterAgentError as exc:
        raise HTTPException(
            status_code=502,
            detail="marketplace credential store unavailable",
        ) from exc


def _load_secret(account: str) -> dict | None:
    value = _auth_secret("get", {"account": account}).get("value")
    return value if isinstance(value, dict) else None


def _store_secret(account: str, value: dict) -> None:
    _auth_secret("store", {"account": account, "value": value})


def _delete_secret(account: str) -> None:
    _auth_secret("delete", {"account": account})


# ─── remote marketplace server access ───


def _marketplace_base_url() -> str:
    value = (
        str(
            os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
            or "https://ofek-dev.com/api/marketplace"
        )
        .strip()
        .rstrip("/")
    )
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=500, detail="marketplace server configuration is invalid"
        )
    return value


def _server_request(
    method: str,
    path: str,
    *,
    access_token: str = "",
    json_body: dict | None = None,
    error_detail: str,
) -> dict:
    headers = dict(_MARKETPLACE_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body: bytes | None = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(
            json_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    if not path.startswith("/") or "://" in path or "\r" in path or "\n" in path:
        raise HTTPException(
            status_code=500, detail="marketplace request configuration is invalid"
        )
    target = f"{_marketplace_base_url()}{path}"
    request = urllib.request.Request(target, data=body, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=15) as response:
            if response.geturl() != target:
                raise HTTPException(status_code=502, detail=error_detail)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(
                status_code=401, detail="marketplace login required"
            ) from exc
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="not found") from exc
        if exc.code == 400:
            raise HTTPException(
                status_code=400, detail="invalid marketplace request"
            ) from exc
        if exc.code == 409:
            raise HTTPException(
                status_code=409, detail="marketplace state changed"
            ) from exc
        if exc.code == 410:
            raise HTTPException(
                status_code=410, detail="marketplace request expired"
            ) from exc
        if exc.code == 429:
            raise HTTPException(
                status_code=429, detail="too many marketplace requests"
            ) from exc
        raise HTTPException(status_code=502, detail=error_detail) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=error_detail) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=502, detail=error_detail)
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=502, detail=error_detail) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=error_detail)
    return payload


# ─── backend-held token lifecycle ───


def _parse_expiry(value: str) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _store_token_response(payload: dict) -> None:
    with _refresh_lock:
        _store_secret(
            _SESSION_ACCOUNT,
            {
                "access_token": str(payload.get("access_token") or ""),
                "access_expires_at": str(payload.get("access_token_expires_at") or ""),
                "refresh_token": str(payload.get("refresh_token") or ""),
            },
        )


def _access_token() -> str:
    tokens = _load_secret(_SESSION_ACCOUNT) or {}
    access = str(tokens.get("access_token") or "")
    expires = _parse_expiry(str(tokens.get("access_expires_at") or ""))
    if access and expires > time.time() + _ACCESS_REFRESH_SLACK_SECONDS:
        return access
    with _refresh_lock:
        current = _load_secret(_SESSION_ACCOUNT) or {}
        current_access = str(current.get("access_token") or "")
        current_expiry = _parse_expiry(str(current.get("access_expires_at") or ""))
        if (
            current_access
            and current_expiry > time.time() + _ACCESS_REFRESH_SLACK_SECONDS
        ):
            return current_access
        current_refresh = str(current.get("refresh_token") or "")
        if not current_refresh:
            if current_access:
                _delete_secret(_SESSION_ACCOUNT)
            return ""
        try:
            payload = _server_request(
                "POST",
                "/auth/app/refresh",
                json_body={"refresh_token": current_refresh},
                error_detail="marketplace auth is unavailable",
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                latest = _load_secret(_SESSION_ACCOUNT) or {}
                latest_refresh = str(latest.get("refresh_token") or "")
                if secrets.compare_digest(latest_refresh, current_refresh):
                    _delete_secret(_SESSION_ACCOUNT)
                return ""
            raise
        _store_token_response(payload)
        return str(payload.get("access_token") or "")


def _require_access_token() -> str:
    token = _access_token()
    if not token:
        raise HTTPException(status_code=401, detail="marketplace login required")
    return token


# ─── pending PKCE requests (state → verifier) ───


def _pending_account(state: str) -> str:
    digest = hashlib.sha256(state.encode("utf-8")).digest()
    return _PENDING_PREFIX + base64.urlsafe_b64encode(digest).decode("ascii").rstrip(
        "="
    )


def _save_pending(state: str, verifier: str) -> None:
    now = time.time()
    for item in _auth_secret("list").get("items", []):
        account = str(item.get("account") or "") if isinstance(item, dict) else ""
        if not account.startswith(_PENDING_PREFIX):
            continue
        pending = _load_secret(account)
        if (
            not pending
            or float(pending.get("created_at") or 0) <= now - _PENDING_TTL_SECONDS
        ):
            _delete_secret(account)
    _store_secret(
        _pending_account(state),
        {"verifier": verifier, "created_at": now},
    )


def _pop_pending(state: str) -> str:
    account = _pending_account(state)
    item = _load_secret(account)
    _delete_secret(account)
    if not isinstance(item, dict):
        return ""
    if float(item.get("created_at") or 0) <= time.time() - _PENDING_TTL_SECONDS:
        return ""
    return str(item.get("verifier") or "")


def _clear_pending() -> None:
    for item in _auth_secret("list").get("items", []):
        account = str(item.get("account") or "") if isinstance(item, dict) else ""
        if account.startswith(_PENDING_PREFIX):
            _delete_secret(account)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ─── catalog ───


def _private_repo_root(context) -> Path:
    raw = str(context.source.get("repo_url") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=500, detail="marketplace source repo is unavailable"
        )
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        return Path(url2pathname(parsed.path)).resolve()
    return Path(raw).resolve()


def _extension_rows(private_root: Path) -> list[dict[str, object]]:
    extensions_root = private_root / "extensions"
    if not extensions_root.is_dir():
        raise HTTPException(
            status_code=500, detail="private extensions directory is unavailable"
        )
    rows: list[dict[str, object]] = []
    for manifest_path in sorted(extensions_root.glob("*/better-agent-extension.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("marketplace") or {}).get("hidden") is True:
            continue
        extension_path = manifest_path.parent.relative_to(private_root).as_posix()
        rows.append(
            {
                "id": str(manifest.get("id") or ""),
                "name": str(manifest.get("name") or ""),
                "version": str(manifest.get("version") or ""),
                "description": str(manifest.get("description") or ""),
                "surfaces": list(manifest.get("surfaces") or []),
                "marketplace": dict(manifest.get("marketplace") or {}),
                "install": {
                    "repo_url": private_root.as_uri(),
                    "extension_path": extension_path,
                    "ref": "",
                },
            }
        )
    return rows


def _ofekdev_catalog(access_token: str) -> tuple[list[dict[str, object]], str]:
    payload = _server_request(
        "GET",
        "/catalog-v1.json",
        access_token=access_token,
        error_detail="marketplace catalog is unavailable",
    )
    rows = []
    for item in payload.get("extensions") or []:
        if not isinstance(item, dict):
            continue
        extension_id = str(item.get("id") or "")
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
    snapshot_id = str(payload.get("snapshot_id") or "")
    if not re.fullmatch(
        r"[A-Za-z0-9._-]{1,80}:[0-9]{1,20}:[a-f0-9]{64}",
        snapshot_id,
    ):
        raise HTTPException(
            status_code=502, detail="marketplace catalog is unavailable"
        )
    return rows, snapshot_id


def _filter_rows(rows: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return rows
    return [
        row
        for row in rows
        if needle in str(row.get("id") or "").lower()
        or needle in str(row.get("name") or "").lower()
        or needle in str(row.get("description") or "").lower()
    ]


_SIGNED_IN_HTML = """<!doctype html>
<title>Better Agent Marketplace</title>
<main style="font-family: system-ui, sans-serif; max-width: 480px; margin: 15vh auto; text-align: center;">
  <h1>%s</h1>
  <p>%s</p>
</main>"""


def create_router(context) -> APIRouter:
    router = APIRouter()

    def _remote_source() -> bool:
        return str(context.source.get("type") or "") in {
            "marketplace",
            "required_artifact",
        }

    @router.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/auth/status")
    async def auth_status() -> dict[str, object]:
        if not _remote_source():
            return {"authenticated": True, "mode": "local", "providers": []}
        token = _access_token()
        if not token:
            providers = _server_request(
                "GET", "/auth/providers", error_detail="marketplace auth is unavailable"
            ).get("providers")
            return {
                "authenticated": False,
                "mode": "remote",
                "providers": list(providers or []),
            }
        me = _server_request(
            "GET",
            "/auth/me",
            access_token=token,
            error_detail="marketplace auth is unavailable",
        )
        return {
            "authenticated": True,
            "mode": "remote",
            "account": me.get("account") or {},
            "providers": list(me.get("providers") or []),
        }

    @router.post("/auth/start")
    async def auth_start(request: Request) -> dict[str, str]:
        body = await request.json()
        provider = str((body or {}).get("provider") or "")
        if provider not in {"google", "apple", "github"}:
            raise HTTPException(status_code=400, detail="unknown login provider")
        verifier, challenge = _pkce_pair()
        base = str(request.base_url).rstrip("/")
        app_redirect = (
            f"{base}/api/extensions/{context.extension_id}/backend/auth/callback"
        )
        with _refresh_lock:
            payload = _server_request(
                "POST",
                "/auth/app/start",
                json_body={
                    "provider": provider,
                    "app_redirect": app_redirect,
                    "code_challenge": challenge,
                },
                error_detail="marketplace auth is unavailable",
            )
            state = str(payload.get("state") or "")
            login_url = str(payload.get("login_url") or "")
            if not state or not login_url:
                raise HTTPException(
                    status_code=502, detail="marketplace auth is unavailable"
                )
            _save_pending(state, verifier)
        return {"login_url": login_url}

    @router.get("/auth/callback")
    async def auth_callback(
        code: str = "", state: str = "", error: str = ""
    ) -> HTMLResponse:
        if error or not code or not state:
            return HTMLResponse(
                _SIGNED_IN_HTML
                % ("Sign-in failed", "Return to Better Agent and try again."),
                status_code=401,
            )
        with _refresh_lock:
            verifier = _pop_pending(state)
            if not verifier:
                return HTMLResponse(
                    _SIGNED_IN_HTML
                    % (
                        "Sign-in failed",
                        "This sign-in link expired. Return to Better Agent and try again.",
                    ),
                    status_code=401,
                )
            payload = _server_request(
                "POST",
                "/auth/app/exchange",
                json_body={"code": code, "code_verifier": verifier},
                error_detail="marketplace auth is unavailable",
            )
            _store_token_response(payload)
        return HTMLResponse(
            _SIGNED_IN_HTML
            % ("Signed in", "You can close this tab and return to Better Agent.")
        )

    @router.post("/auth/logout")
    async def auth_logout() -> dict[str, bool]:
        with _refresh_lock:
            _clear_pending()
            tokens = _load_secret(_SESSION_ACCOUNT) or {}
            refresh = str(tokens.get("refresh_token") or "")
            if refresh:
                try:
                    _server_request(
                        "POST",
                        "/auth/app/logout",
                        json_body={"refresh_token": refresh},
                        error_detail="marketplace auth is unavailable",
                    )
                except HTTPException:
                    pass  # local sign-out must succeed even when the server is unreachable
            _delete_secret(_SESSION_ACCOUNT)
        return {"ok": True}

    @router.post("/auth/protocol-ready")
    async def auth_protocol_ready() -> dict[str, bool]:
        return {"authenticated": bool(_require_access_token())}

    @router.get("/catalog")
    async def catalog(q: str = "") -> dict[str, object]:
        if _remote_source():
            rows, snapshot_id = _ofekdev_catalog(_require_access_token())
        else:
            rows = _extension_rows(_private_repo_root(context))
            snapshot_id = ""
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "protocol_hash": _PROTOCOL_HASH,
            "snapshot_id": snapshot_id,
            "extensions": _filter_rows(rows, q),
        }

    @router.get("/metadata/{extension_id}")
    async def metadata(extension_id: str) -> dict[str, object]:
        return _server_request(
            "GET",
            f"/extensions/{urllib.parse.quote(extension_id, safe='')}/metadata",
            access_token=_require_access_token(),
            error_detail="marketplace metadata is unavailable",
        )

    @router.get("/billing/config")
    async def billing_config() -> dict[str, object]:
        return _server_request(
            "GET", "/billing/config", error_detail="billing is unavailable"
        )

    @router.post("/billing/checkout")
    async def billing_checkout(request: Request) -> dict[str, object]:
        body = await request.json()
        product_id = str((body or {}).get("product_id") or "")
        if not product_id:
            raise HTTPException(status_code=400, detail="product_id is required")
        return _server_request(
            "POST",
            "/billing/checkout",
            access_token=_require_access_token(),
            json_body={"product_id": product_id},
            error_detail="billing is unavailable",
        )

    @router.get("/billing/entitlement/{product_id}")
    async def billing_entitlement(product_id: str) -> dict[str, object]:
        return _server_request(
            "GET",
            f"/billing/entitlement/{urllib.parse.quote(product_id, safe='')}",
            access_token=_require_access_token(),
            error_detail="billing is unavailable",
        )

    return router
