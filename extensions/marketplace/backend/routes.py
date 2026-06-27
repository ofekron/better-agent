from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import RedirectResponse

_MARKETPLACE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BetterAgent/marketplace-extension",
}

# Extension ids are namespace.name over [A-Za-z0-9._-]. Anything else is rejected
# before being interpolated into a hosted URL path so a crafted id cannot inject
# path segments ("../") or query fragments ("?", "#") into the marketplace request.
_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _marketplace_base_url() -> str:
    return str(os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL") or "https://ofek-dev.com/api/marketplace").strip().rstrip("/")


def _require_extension_id(extension_id: str) -> str:
    clean = str(extension_id or "").strip()
    if not _EXTENSION_ID_RE.fullmatch(clean):
        raise HTTPException(status_code=400, detail="invalid extension id")
    return clean


def _provider_login_url(provider: str) -> str:
    if provider not in {"google", "apple", "github"}:
        raise HTTPException(status_code=404, detail="unknown login provider")
    return f"{_marketplace_base_url()}/auth/login/{provider}"


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


def _bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    value = str(authorization or "")
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


def create_router(context) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/auth/providers")
    async def auth_providers() -> dict[str, object]:
        return {
            "providers": [
                {"id": "google", "label": "Google", "login_url": _provider_login_url("google")},
                {"id": "apple", "label": "Apple", "login_url": _provider_login_url("apple")},
                {"id": "github", "label": "GitHub", "login_url": _provider_login_url("github")},
            ]
        }

    @router.get("/auth/login/{provider}")
    async def auth_login(provider: str):
        return RedirectResponse(_provider_login_url(provider), status_code=302)

    @router.get("/catalog")
    async def catalog(authorization: str = Header(default="")) -> dict[str, object]:
        return {"extensions": _ofekdev_rows(_bearer_token(authorization))}

    @router.get("/metadata/{extension_id}")
    async def metadata(extension_id: str, authorization: str = Header(default="")) -> dict[str, object]:
        return _ofekdev_metadata(extension_id, _bearer_token(authorization))

    @router.post("/extensions/{extension_id}/uninstall")
    async def extension_uninstall(extension_id: str, authorization: str = Header(default="")) -> dict[str, bool]:
        _ofekdev_uninstall(extension_id, _bearer_token(authorization))
        return {"ok": True}

    return router
