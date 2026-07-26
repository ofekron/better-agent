from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

_MARKETPLACE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BetterAgent/marketplace-extension",
}

_TOKENS_KEY = "auth_tokens"
_PENDING_KEY = "auth_pending"
_PENDING_TTL_SECONDS = 600
_ACCESS_REFRESH_SLACK_SECONDS = 60
_PROTOCOL_VERSION = 1
_PROTOCOL_HASH = "425ec53e50f2bb093bb79cf2c2090a61a8c752cc44b93b731a7fa0ffc5430d8f"


# ─── extension storage (backend-held tokens; never exposed to the iframe) ───


def _storage_client():
    from better_agent_sdk import Client

    return Client()


def _storage_load_json(key: str) -> dict:
    raw = _storage_client().storage_get_bytes(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _storage_save_json(key: str, value: dict) -> None:
    _storage_client().storage_put(key, json.dumps(value, separators=(",", ":")))


def _storage_delete(key: str) -> None:
    _storage_client().storage_delete(key)


# ─── remote marketplace server access ───


def _marketplace_base_url() -> str:
    value = str(
        os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
        or "https://ofek-dev.com/api/marketplace"
    ).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=500, detail="marketplace server configuration is invalid")
    return value


def _server_request(
    method: str,
    path: str,
    *,
    access_token: str = "",
    json_body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
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
    for key, value in (extra_headers or {}).items():
        if key not in {"X-BA-Device-Challenge", "X-BA-Device-Signature"}:
            raise HTTPException(status_code=500, detail="marketplace request configuration is invalid")
        headers[key] = value
    if not path.startswith("/") or "://" in path or "\r" in path or "\n" in path:
        raise HTTPException(status_code=500, detail="marketplace request configuration is invalid")
    request = urllib.request.Request(
        f"{_marketplace_base_url()}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(status_code=401, detail="marketplace login required") from exc
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="not found") from exc
        if exc.code == 400:
            raise HTTPException(status_code=400, detail="invalid marketplace request") from exc
        if exc.code == 409:
            raise HTTPException(status_code=409, detail="marketplace state changed") from exc
        if exc.code == 410:
            raise HTTPException(status_code=410, detail="marketplace request expired") from exc
        if exc.code == 429:
            raise HTTPException(status_code=429, detail="too many marketplace requests") from exc
        raise HTTPException(status_code=502, detail=error_detail) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=error_detail) from exc
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
    from datetime import datetime

    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _store_token_response(payload: dict) -> None:
    _storage_save_json(
        _TOKENS_KEY,
        {
            "access_token": str(payload.get("access_token") or ""),
            "access_expires_at": str(payload.get("access_token_expires_at") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
        },
    )


def _access_token() -> str:
    """Current access token, transparently refreshed; empty when signed out."""
    tokens = _storage_load_json(_TOKENS_KEY)
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")
    if not access and not refresh:
        return ""
    expires = _parse_expiry(str(tokens.get("access_expires_at") or ""))
    if access and expires > time.time() + _ACCESS_REFRESH_SLACK_SECONDS:
        return access
    if not refresh:
        _storage_delete(_TOKENS_KEY)
        return ""
    try:
        payload = _server_request(
            "POST",
            "/auth/app/refresh",
            json_body={"refresh_token": refresh},
            error_detail="marketplace auth is unavailable",
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            _storage_delete(_TOKENS_KEY)
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


def _state_key(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _save_pending(state: str, verifier: str) -> None:
    pending = _storage_load_json(_PENDING_KEY)
    now = time.time()
    pending = {
        key: item
        for key, item in pending.items()
        if isinstance(item, dict) and float(item.get("created_at") or 0) > now - _PENDING_TTL_SECONDS
    }
    pending[_state_key(state)] = {"verifier": verifier, "created_at": now}
    _storage_save_json(_PENDING_KEY, pending)


def _pop_pending(state: str) -> str:
    pending = _storage_load_json(_PENDING_KEY)
    item = pending.pop(_state_key(state), None)
    _storage_save_json(_PENDING_KEY, pending)
    if not isinstance(item, dict):
        return ""
    if float(item.get("created_at") or 0) <= time.time() - _PENDING_TTL_SECONDS:
        return ""
    return str(item.get("verifier") or "")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ─── protocol-v1 transport ───


def _strict_object(
    value: object,
    allowed: set[str],
    required: set[str],
    detail: str,
) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise HTTPException(status_code=400, detail=detail)
    return value


def _strict_protocol_string(body: dict, field: str, pattern: str, detail: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise HTTPException(status_code=400, detail=detail)
    return value


def _require_protocol_hash(body: dict, detail: str) -> None:
    if body.get("protocol_hash") != _PROTOCOL_HASH:
        raise HTTPException(status_code=409, detail=detail)


def _signed_protocol_request(
    method: str,
    path: str,
    body: dict,
    *,
    error_detail: str,
) -> dict:
    signed = _strict_object(
        body,
        set(body),
        {"challenge", "signature"},
        "invalid signed marketplace request",
    )
    challenge = _strict_protocol_string(
        signed,
        "challenge",
        r"bachal_[A-Za-z0-9_-]{43}",
        "invalid signed marketplace request",
    )
    signature = _strict_protocol_string(
        signed,
        "signature",
        r"[A-Za-z0-9_-]{86}",
        "invalid signed marketplace request",
    )
    payload = {
        key: value for key, value in signed.items() if key not in {"challenge", "signature"}
    }
    return _server_request(
        method,
        path,
        access_token=_require_access_token(),
        json_body=payload,
        extra_headers={
            "X-BA-Device-Challenge": challenge,
            "X-BA-Device-Signature": signature,
        },
        error_detail=error_detail,
    )


# ─── catalog ───


def _private_repo_root(context) -> Path:
    raw = str(context.source.get("repo_url") or "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail="marketplace source repo is unavailable")
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        return Path(url2pathname(parsed.path)).resolve()
    return Path(raw).resolve()


def _extension_rows(private_root: Path) -> list[dict[str, object]]:
    extensions_root = private_root / "extensions"
    if not extensions_root.is_dir():
        raise HTTPException(status_code=500, detail="private extensions directory is unavailable")
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
        raise HTTPException(status_code=502, detail="marketplace catalog is unavailable")
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
        return str(context.source.get("type") or "") in {"marketplace", "required_artifact"}

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
            return {"authenticated": False, "mode": "remote", "providers": list(providers or [])}
        me = _server_request(
            "GET", "/auth/me", access_token=token, error_detail="marketplace auth is unavailable"
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
        app_redirect = f"{base}/api/extensions/{context.extension_id}/backend/auth/callback"
        payload = _server_request(
            "POST",
            "/auth/app/start",
            json_body={"provider": provider, "app_redirect": app_redirect, "code_challenge": challenge},
            error_detail="marketplace auth is unavailable",
        )
        state = str(payload.get("state") or "")
        login_url = str(payload.get("login_url") or "")
        if not state or not login_url:
            raise HTTPException(status_code=502, detail="marketplace auth is unavailable")
        _save_pending(state, verifier)
        return {"login_url": login_url}

    @router.get("/auth/callback")
    async def auth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
        if error or not code or not state:
            return HTMLResponse(
                _SIGNED_IN_HTML % ("Sign-in failed", "Return to Better Agent and try again."), status_code=401
            )
        verifier = _pop_pending(state)
        if not verifier:
            return HTMLResponse(
                _SIGNED_IN_HTML % ("Sign-in failed", "This sign-in link expired. Return to Better Agent and try again."),
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
            _SIGNED_IN_HTML % ("Signed in", "You can close this tab and return to Better Agent.")
        )

    @router.post("/auth/logout")
    async def auth_logout() -> dict[str, bool]:
        tokens = _storage_load_json(_TOKENS_KEY)
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
        _storage_delete(_TOKENS_KEY)
        return {"ok": True}

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

    @router.post("/protocol/v1/pair/context")
    async def protocol_pair_context(request: Request) -> dict[str, object]:
        body = _strict_object(
            await request.json(),
            {"pair_token", "protocol_hash"},
            {"pair_token", "protocol_hash"},
            "invalid pairing request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        pair_token = _strict_protocol_string(
            body,
            "pair_token",
            r"[A-Za-z0-9_-]{43}",
            "invalid pairing request",
        )
        return _server_request(
            "POST",
            "/protocol/v1/pair/context",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace pairing is unavailable",
        )

    @router.post("/protocol/v1/pair/redeem")
    async def protocol_pair(request: Request) -> dict[str, object]:
        body = _strict_object(
            await request.json(),
            {
                "pair_token",
                "protocol_hash",
                "device_id",
                "public_key",
                "pop_signature",
                "label",
            },
            {
                "pair_token",
                "protocol_hash",
                "device_id",
                "public_key",
                "pop_signature",
                "label",
            },
            "invalid pairing request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        for field, pattern in {
            "pair_token": r"[A-Za-z0-9_-]{43}",
            "device_id": r"badvc_[a-f0-9]{32}",
            "public_key": r"[A-Za-z0-9_-]{43}",
            "pop_signature": r"[A-Za-z0-9_-]{86}",
        }.items():
            _strict_protocol_string(body, field, pattern, "invalid pairing request")
        label = body["label"]
        if (
            not isinstance(label, str)
            or not 1 <= len(label.strip()) <= 80
            or any(ord(character) < 32 or ord(character) == 127 for character in label)
        ):
            raise HTTPException(status_code=400, detail="invalid pairing request")
        return _server_request(
            "POST",
            "/protocol/v1/pair/redeem",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace pairing is unavailable",
        )

    @router.post("/protocol/v1/pair/reject")
    async def protocol_pair_reject(request: Request) -> dict[str, object]:
        body = _strict_object(
            await request.json(),
            {"pair_token", "protocol_hash"},
            {"pair_token", "protocol_hash"},
            "invalid pairing rejection",
        )
        _require_protocol_hash(body, "marketplace client update required")
        _strict_protocol_string(
            body, "pair_token", r"[A-Za-z0-9_-]{43}", "invalid pairing rejection"
        )
        return _server_request(
            "POST",
            "/protocol/v1/pair/reject",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace pairing is unavailable",
        )

    @router.post("/protocol/v1/devices/{device_id}/challenges")
    async def protocol_challenge(device_id: str, request: Request) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id):
            raise HTTPException(status_code=400, detail="invalid device id")
        body = _strict_object(
            await request.json(),
            {"protocol_hash"},
            {"protocol_hash"},
            "invalid challenge request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        return _server_request(
            "POST",
            f"/protocol/v1/devices/{device_id}/challenges",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace device authentication is unavailable",
        )

    @router.post("/protocol/v1/devices/{device_id}/actions/lease")
    async def protocol_actions_lease(
        device_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id):
            raise HTTPException(status_code=400, detail="invalid device id")
        body = _strict_object(
            await request.json(),
            {"protocol_hash", "wait_seconds", "challenge", "signature"},
            {"protocol_hash", "wait_seconds", "challenge", "signature"},
            "invalid lease request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        if (
            not isinstance(body["wait_seconds"], int)
            or isinstance(body["wait_seconds"], bool)
            or not 0 <= body["wait_seconds"] <= 30
        ):
            raise HTTPException(status_code=400, detail="invalid lease request")
        return _signed_protocol_request(
            "POST",
            f"/protocol/v1/devices/{device_id}/actions/lease",
            body,
            error_detail="marketplace action polling is unavailable",
        )

    @router.post("/protocol/v1/devices/{device_id}/actions/{action_id}/fence")
    async def protocol_action_fence(
        device_id: str, action_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id) or not re.fullmatch(
            r"baact_[a-f0-9]{32}", action_id
        ):
            raise HTTPException(status_code=400, detail="invalid fence request")
        body = _strict_object(
            await request.json(),
            {
                "protocol_hash",
                "lease_capability",
                "envelope_digest",
                "receipt_revision",
                "challenge",
                "signature",
            },
            {
                "protocol_hash",
                "lease_capability",
                "envelope_digest",
                "receipt_revision",
                "challenge",
                "signature",
            },
            "invalid fence request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        _strict_protocol_string(
            body, "lease_capability", r"balease_[A-Za-z0-9_-]{43}", "invalid fence request"
        )
        _strict_protocol_string(
            body, "envelope_digest", r"[a-f0-9]{64}", "invalid fence request"
        )
        if (
            not isinstance(body["receipt_revision"], int)
            or isinstance(body["receipt_revision"], bool)
            or body["receipt_revision"] < 1
        ):
            raise HTTPException(status_code=400, detail="invalid fence request")
        return _signed_protocol_request(
            "POST",
            f"/protocol/v1/devices/{device_id}/actions/{action_id}/fence",
            body,
            error_detail="marketplace action fencing is unavailable",
        )

    @router.post("/protocol/v1/devices/{device_id}/actions/{action_id}/reject")
    async def protocol_action_reject(
        device_id: str, action_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id) or not re.fullmatch(
            r"baact_[a-f0-9]{32}", action_id
        ):
            raise HTTPException(status_code=400, detail="invalid rejection")
        body = _strict_object(
            await request.json(),
            {
                "protocol_hash",
                "lease_capability",
                "envelope_digest",
                "challenge",
                "signature",
            },
            {
                "protocol_hash",
                "lease_capability",
                "envelope_digest",
                "challenge",
                "signature",
            },
            "invalid rejection",
        )
        _require_protocol_hash(body, "marketplace client update required")
        _strict_protocol_string(
            body, "lease_capability", r"balease_[A-Za-z0-9_-]{43}", "invalid rejection"
        )
        _strict_protocol_string(
            body, "envelope_digest", r"[a-f0-9]{64}", "invalid rejection"
        )
        return _signed_protocol_request(
            "POST",
            f"/protocol/v1/devices/{device_id}/actions/{action_id}/reject",
            body,
            error_detail="marketplace action rejection is unavailable",
        )

    @router.post("/protocol/v1/actions/{action_id}/terminal-ack")
    async def protocol_action_ack(
        action_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"baact_[a-f0-9]{32}", action_id):
            raise HTTPException(status_code=400, detail="invalid acknowledgement")
        body = _strict_object(
            await request.json(),
            {
                "terminal_capability",
                "envelope_digest",
                "outcome",
                "result_code",
                "receipt_revision",
            },
            {
                "terminal_capability",
                "envelope_digest",
                "outcome",
                "result_code",
                "receipt_revision",
            },
            "invalid acknowledgement",
        )
        _strict_protocol_string(
            body,
            "terminal_capability",
            r"baterm_[A-Za-z0-9_-]{43}",
            "invalid acknowledgement",
        )
        _strict_protocol_string(
            body, "envelope_digest", r"[a-f0-9]{64}", "invalid acknowledgement"
        )
        _strict_protocol_string(
            body,
            "outcome",
            r"(?:succeeded|failed|failed_unknown)",
            "invalid acknowledgement",
        )
        _strict_protocol_string(
            body, "result_code", r"[a-z0-9_]{1,80}", "invalid acknowledgement"
        )
        if (
            not isinstance(body["receipt_revision"], int)
            or isinstance(body["receipt_revision"], bool)
            or body["receipt_revision"] < 1
        ):
            raise HTTPException(status_code=400, detail="invalid acknowledgement")
        return _server_request(
            "POST",
            f"/protocol/v1/actions/{action_id}/terminal-ack",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace action acknowledgement is unavailable",
        )

    @router.put("/protocol/v1/devices/{device_id}/projection")
    async def protocol_projection(
        device_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id):
            raise HTTPException(status_code=400, detail="invalid projection")
        body = _strict_object(
            await request.json(),
            {"protocol_hash", "revision", "extensions", "challenge", "signature"},
            {"protocol_hash", "revision", "extensions", "challenge", "signature"},
            "invalid projection",
        )
        _require_protocol_hash(body, "marketplace client update required")
        if (
            not isinstance(body["revision"], int)
            or isinstance(body["revision"], bool)
            or body["revision"] < 0
            or not isinstance(body["extensions"], list)
        ):
            raise HTTPException(status_code=400, detail="invalid projection")
        return _signed_protocol_request(
            "PUT",
            f"/protocol/v1/devices/{device_id}/projection",
            body,
            error_detail="marketplace projection update is unavailable",
        )

    @router.post("/protocol/v1/catalog/resolve")
    async def protocol_catalog_resolve(request: Request) -> dict[str, object]:
        body = _strict_object(
            await request.json(),
            {"protocol_hash", "snapshot_id", "extension_id"},
            {"protocol_hash", "snapshot_id", "extension_id"},
            "invalid catalog request",
        )
        _require_protocol_hash(body, "marketplace client update required")
        _strict_protocol_string(
            body,
            "snapshot_id",
            r"[A-Za-z0-9._-]{1,80}:[0-9]{1,20}:[a-f0-9]{64}",
            "invalid catalog request",
        )
        _strict_protocol_string(
            body,
            "extension_id",
            r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?",
            "invalid catalog request",
        )
        return _server_request(
            "POST",
            "/protocol/v1/catalog/resolve",
            access_token=_require_access_token(),
            json_body=body,
            error_detail="marketplace catalog is unavailable",
        )

    @router.post("/protocol/v1/devices/{device_id}/revoke")
    async def protocol_revoke(device_id: str, request: Request) -> dict[str, object]:
        if not re.fullmatch(r"badvc_[a-f0-9]{32}", device_id):
            raise HTTPException(status_code=400, detail="invalid revocation")
        body = _strict_object(
            await request.json(),
            {"protocol_hash", "challenge", "signature"},
            {"protocol_hash", "challenge", "signature"},
            "invalid revocation",
        )
        _require_protocol_hash(body, "marketplace client update required")
        return _signed_protocol_request(
            "POST",
            f"/protocol/v1/devices/{device_id}/revoke",
            body,
            error_detail="marketplace revocation is unavailable",
        )

    @router.post("/protocol/actions/{action_id}/metadata")
    async def protocol_action_metadata(
        action_id: str, request: Request
    ) -> dict[str, object]:
        if not re.fullmatch(r"baact_[a-f0-9]{32}", action_id):
            raise HTTPException(status_code=400, detail="invalid action id")
        _strict_object(await request.json(), set(), set(), "invalid metadata request")
        return _server_request(
            "POST",
            f"/protocol/v1/actions/{action_id}/metadata",
            access_token=_require_access_token(),
            json_body={},
            error_detail="marketplace action metadata is unavailable",
        )

    @router.get("/metadata/{extension_id}")
    async def metadata(extension_id: str) -> dict[str, object]:
        return _server_request(
            "GET",
            f"/extensions/{urllib.parse.quote(extension_id, safe='')}/metadata",
            access_token=_require_access_token(),
            error_detail="marketplace metadata is unavailable",
        )

    @router.post("/extensions/{extension_id}/uninstall")
    async def extension_uninstall(extension_id: str) -> dict[str, bool]:
        if not _remote_source():
            return {"ok": True}
        _server_request(
            "POST",
            f"/extensions/{urllib.parse.quote(extension_id, safe='')}/uninstall",
            access_token=_require_access_token(),
            error_detail="marketplace uninstall is unavailable",
        )
        return {"ok": True}

    @router.get("/billing/config")
    async def billing_config() -> dict[str, object]:
        return _server_request("GET", "/billing/config", error_detail="billing is unavailable")

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
