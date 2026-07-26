from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from fastapi import HTTPException

_DEFAULT_ORIGIN = "https://ofek-dev.com/api/marketplace"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BetterAgent/marketplace-core",
}
_CHALLENGE_PATTERN = re.compile(r"^bachal_[A-Za-z0-9_-]{43}$")
_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin() -> str:
    value = (
        str(os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL") or _DEFAULT_ORIGIN)
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
        or parsed.params
        or "%" in parsed.path
        or "\\" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise HTTPException(
            status_code=500,
            detail="marketplace server configuration is invalid",
        )
    return value


def request(
    method: str,
    path: str,
    *,
    access_token: str,
    body: dict,
    signed: bool = False,
) -> dict:
    if (
        method not in {"POST", "PUT"}
        or not path.startswith("/protocol/v1/")
        or "://" in path
        or "?" in path
        or "#" in path
        or "\r" in path
        or "\n" in path
        or not isinstance(body, dict)
    ):
        raise HTTPException(
            status_code=500,
            detail="marketplace protocol request is invalid",
        )
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 4096
        or any(
            ord(character) < 33 or ord(character) == 127 for character in access_token
        )
    ):
        raise HTTPException(status_code=401, detail="marketplace login required")

    payload = dict(body)
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    headers["Content-Type"] = "application/json"
    if signed:
        challenge = payload.pop("challenge", None)
        signature = payload.pop("signature", None)
        if (
            not isinstance(challenge, str)
            or _CHALLENGE_PATTERN.fullmatch(challenge) is None
            or not isinstance(signature, str)
            or _SIGNATURE_PATTERN.fullmatch(signature) is None
        ):
            raise HTTPException(
                status_code=500,
                detail="marketplace protocol signature is invalid",
            )
        headers["X-BA-Device-Challenge"] = challenge
        headers["X-BA-Device-Signature"] = signature

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    target = f"{origin()}{path}"
    request_value = urllib.request.Request(
        target,
        data=encoded,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(
            request_value,
            timeout=15,
        ) as response:
            if response.geturl() != target:
                raise HTTPException(
                    status_code=502,
                    detail="marketplace protocol origin changed",
                )
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = {
            400: 400,
            401: 401,
            404: 404,
            409: 409,
            410: 410,
            429: 429,
        }.get(exc.code, 502)
        detail = {
            400: "invalid marketplace request",
            401: "marketplace login required",
            404: "marketplace resource not found",
            409: "marketplace state changed",
            410: "marketplace request expired",
            429: "too many marketplace requests",
        }.get(exc.code, "marketplace protocol is unavailable")
        raise HTTPException(status_code=status, detail=detail) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(
            status_code=502,
            detail="marketplace protocol is unavailable",
        ) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=502,
            detail="marketplace protocol response is too large",
        )
    try:
        payload_value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="marketplace protocol returned invalid JSON",
        ) from exc
    if not isinstance(payload_value, dict):
        raise HTTPException(
            status_code=502,
            detail="marketplace protocol returned an invalid response",
        )
    return payload_value
