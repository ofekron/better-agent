from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import extension_store
from extension_backend_loader import invoke_extension_backend_sync
from paths import ba_home
from provider_manifest import spec_for


CONTRACT_VERSION = 1
_MAX_CA_BYTES = 64 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_CA_ENV_KEYS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
_LOCK = threading.Lock()


class ProviderTransportError(RuntimeError):
    pass


def _loopback_http_url(value: Any, field: str, *, allow_path: bool) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
    ):
        raise ProviderTransportError(f"provider transport returned invalid {field}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderTransportError(f"provider transport returned invalid {field}") from exc
    if port is None or not 1 <= port <= 65535:
        raise ProviderTransportError(f"provider transport returned invalid {field}")
    return raw.rstrip("/")


def _ca_path(pem_value: Any, fingerprint_value: Any) -> Path:
    if not isinstance(pem_value, str):
        raise ProviderTransportError("provider transport returned no CA certificate")
    pem = pem_value.encode("ascii", "strict")
    if not pem or len(pem) > _MAX_CA_BYTES:
        raise ProviderTransportError("provider transport returned invalid CA certificate size")
    if not pem.startswith(b"-----BEGIN CERTIFICATE-----\n") or not pem.rstrip().endswith(
        b"-----END CERTIFICATE-----"
    ):
        raise ProviderTransportError("provider transport returned invalid CA certificate")
    fingerprint = hashlib.sha256(pem).hexdigest()
    if str(fingerprint_value or "").lower() != fingerprint:
        raise ProviderTransportError("provider transport CA fingerprint mismatch")
    target_dir = ba_home() / "runtime" / "provider-transport"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target_dir.is_symlink():
        raise ProviderTransportError("provider transport CA directory is unsafe")
    target = target_dir / f"{fingerprint}.pem"
    with _LOCK:
        if target.is_symlink():
            raise ProviderTransportError("provider transport CA path is unsafe")
        if not target.exists() or target.read_bytes() != pem:
            with tempfile.NamedTemporaryFile(dir=target_dir, prefix="ca-", delete=False) as handle:
                temp = Path(handle.name)
                handle.write(pem)
            try:
                os.chmod(temp, 0o600)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        os.chmod(target, 0o600)
    return target


def _merge_no_proxy(env: dict[str, str]) -> None:
    required = ("127.0.0.1", "localhost", "::1")
    current = str(env.get("NO_PROXY") or env.get("no_proxy") or "")
    parts = [item.strip() for item in current.split(",") if item.strip()]
    lowered = {item.lower() for item in parts}
    parts.extend(item for item in required if item.lower() not in lowered)
    value = ",".join(parts)
    env["NO_PROXY"] = value
    env["no_proxy"] = value


def _request_payload(
    env: dict[str, str],
    *,
    provider_id: str,
    provider_kind: str,
    provider_mode: str,
) -> bytes:
    spec = spec_for(provider_kind)
    gateway_env = spec.transport_gateway_env if spec else None
    upstream = str(env.get(gateway_env) or "") if gateway_env else ""
    if not upstream and spec:
        upstream = str(spec.transport_default_base_url or "")
    if upstream:
        parsed = urlsplit(upstream)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderTransportError("provider upstream base URL is unsafe to disclose")
    return json.dumps(
        {
            "version": CONTRACT_VERSION,
            "provider_id": provider_id,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "gateway_env": gateway_env or "",
            "upstream_base_url": upstream,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def apply_provider_transport(
    env: dict[str, str],
    *,
    provider_id: str,
    provider_kind: str,
    provider_mode: str,
) -> dict[str, str]:
    hooks = extension_store.provider_transport_hooks()
    if not hooks:
        return env
    if len(hooks) != 1:
        raise ProviderTransportError("exactly one active provider transport extension is required")
    extension_id, path = hooks[0]
    status, body = invoke_extension_backend_sync(
        extension_id,
        path,
        body_bytes=_request_payload(
            env,
            provider_id=provider_id,
            provider_kind=provider_kind,
            provider_mode=provider_mode,
        ),
    )
    if status != 200:
        raise ProviderTransportError(f"provider transport extension is unavailable ({status})")
    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError) as exc:
        raise ProviderTransportError("provider transport returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != CONTRACT_VERSION:
        raise ProviderTransportError("provider transport contract version mismatch")
    if payload.get("enabled") is not True:
        return env

    proxy_url = _loopback_http_url(
        payload.get("forward_proxy_url"), "forward_proxy_url", allow_path=False
    )
    ca_path = _ca_path(payload.get("ca_certificate_pem"), payload.get("ca_sha256"))
    result = dict(env)
    for key in _PROXY_ENV_KEYS:
        result[key] = proxy_url
    for key in _CA_ENV_KEYS:
        result[key] = str(ca_path)
    _merge_no_proxy(result)

    spec = spec_for(provider_kind)
    if spec and spec.transport_gateway_env:
        gateway_url = _loopback_http_url(
            payload.get("gateway_base_url"), "gateway_base_url", allow_path=True
        )
        result[spec.transport_gateway_env] = gateway_url
    return result
