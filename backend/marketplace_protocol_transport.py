from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from fastapi import HTTPException

from marketplace_protocol import PATTERNS, PROTOCOL

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SIGNED_OPERATIONS = frozenset({"lease", "fence", "reject", "projection", "revoke"})
_PATH_PARAMETER_PATTERN = re.compile(r"\{([a-z_]+)\}")
_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")
_PATH_IDENTIFIER_KINDS = {
    "action_id": "action",
    "device_id": "device",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def origin() -> str:
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
        or parsed.params
        or "%" in parsed.path
        or "\\" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise HTTPException(
            status_code=500,
            detail="Marketplace server configuration is invalid",
        )
    return value


def _operation_path(operation: str, values: dict[str, str]) -> tuple[str, str, set[str]]:
    spec = PROTOCOL["http"].get(operation)
    if not isinstance(spec, dict):
        raise HTTPException(status_code=500, detail="Marketplace operation is invalid")
    path = str(spec["path"])
    parameters = set(_PATH_PARAMETER_PATTERN.findall(path))
    if set(values) != parameters:
        raise HTTPException(status_code=500, detail="Marketplace path is incomplete")
    for name, value in values.items():
        kind = _PATH_IDENTIFIER_KINDS.get(name)
        if kind is None or not isinstance(value, str) or not PATTERNS[kind].fullmatch(value):
            raise HTTPException(status_code=400, detail="Marketplace identifier is invalid")
        path = path.replace(f"{{{name}}}", quote(value, safe=""))
    return str(spec["method"]), path, set(spec["request"])


def _request_sync(
    method: str,
    path: str,
    body: dict,
    access_token: str,
    signed: bool,
) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "BetterAgent/marketplace-protocol-v1",
    }
    payload = dict(body)
    if signed:
        challenge = payload.pop("challenge")
        signature = payload.pop("signature")
        if (
            not isinstance(challenge, str)
            or not PATTERNS["challenge"].fullmatch(challenge)
            or not isinstance(signature, str)
            or not _SIGNATURE_PATTERN.fullmatch(signature)
        ):
            raise HTTPException(
                status_code=400,
                detail="Marketplace device proof is invalid",
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
    request = urllib.request.Request(
        target,
        data=encoded,
        headers=headers,
        method=method,
    )
    timeout = 40 if path.endswith("/actions/lease") else 15
    try:
        with urllib.request.build_opener(_NoRedirect).open(
            request,
            timeout=timeout,
        ) as response:
            if response.geturl() != target:
                raise HTTPException(status_code=502, detail="Marketplace redirect refused")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code if exc.code in {400, 401, 403, 404, 409, 410, 429} else 502
        raise HTTPException(status_code=status, detail="Marketplace protocol request failed") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Marketplace protocol unavailable") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=502, detail="Marketplace response is too large")
    if not raw:
        return {}
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Marketplace returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Marketplace returned an invalid response")
    return result


def request(
    operation: str,
    path_values: dict[str, str],
    *,
    access_token: str,
    body: dict,
) -> dict:
    method, path, request_fields = _operation_path(operation, path_values)
    response_fields = set(PROTOCOL["http"][operation]["response"])
    if set(body) != request_fields:
        raise HTTPException(
            status_code=400,
            detail="Marketplace protocol request has an invalid shape",
        )
    signed = operation in _SIGNED_OPERATIONS
    if signed != ({"challenge", "signature"} <= set(body)):
        raise HTTPException(
            status_code=400,
            detail="Marketplace device proof has an invalid shape",
        )
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 8192
        or any(ord(character) < 33 or ord(character) == 127 for character in access_token)
    ):
        raise HTTPException(status_code=401, detail="Marketplace login required")
    result = _request_sync(
        method,
        path,
        body,
        access_token,
        signed,
    )
    if set(result) != response_fields:
        raise HTTPException(
            status_code=502,
            detail="Marketplace protocol response has an invalid shape",
        )
    return result
