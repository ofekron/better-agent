"""Operation descriptor — the frozen, provider-authored template.

A descriptor is the *only* thing that decides what the broker does with a
secret. The provider authors it; the user approves it; the broker stores
its own copy and executes exactly that. It therefore crosses a trust
boundary and is validated strictly — unexpected shapes are rejected, never
coerced (CLAUDE.md: "Reject unexpected shapes; do not coerce or guess").

Secrets are referenced by placeholders inside template strings:
``{{secret}}`` for the default single secret, or ``{{secret:name}}`` for
named multi-secret operations. Raw values are NEVER part of a descriptor.
The descriptor does not name stored secret refs either — the user binds
concrete values to the consent at approval time, so the provider can
propose an operation without choosing which stored secret fills it.

New sink kinds register their own schema fragment here and an executor
under ``executors/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

SECRET_PLACEHOLDER = "{{secret}}"
DEFAULT_SECRET_NAME = "secret"
SECRET_NAME_RE = r"[A-Za-z0-9_.-]{1,64}"
SECRET_PLACEHOLDER_RE = re.compile(r"\{\{secret(?::(" + SECRET_NAME_RE + r"))?\}\}")
SECRET_SOURCE_KINDS = ("password_manager",)

SUPPORTED_SINK_KINDS = ("http", "local_keychain", "exec")

# Methods we treat as side-effect-free. Anything else (or unknown) is
# classified high-risk and requires the per-op presence gate — fail-closed.
IDEMPOTENT_HTTP_METHODS = ("GET", "HEAD", "OPTIONS")


class DescriptorError(ValueError):
    """Raised when a provider-supplied descriptor is malformed."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise DescriptorError(msg)


def _require_str(d: dict, key: str, *, max_len: int = 4096) -> str:
    val = d.get(key)
    _require(isinstance(val, str), f"{key!r} must be a string")
    _require(0 < len(val) <= max_len, f"{key!r} length out of range")
    return val


def _validate_secret_sources(raw: Any, secret_names: list[str]) -> dict[str, dict]:
    if raw is None:
        return {}
    _require(isinstance(raw, dict), "'secret_sources' must be an object")
    secret_name_set = set(secret_names)
    out: dict[str, dict] = {}
    for name, source in raw.items():
        _require(isinstance(name, str), "secret source names must be strings")
        _require(name in secret_name_set, f"secret source {name!r} has no matching placeholder")
        _require(isinstance(source, dict), f"secret source {name!r} must be an object")
        unexpected = set(source) - {"kind", "service", "account"}
        _require(not unexpected, f"secret source {name!r} has unexpected fields")
        kind = source.get("kind")
        _require(kind in SECRET_SOURCE_KINDS, f"secret source {name!r} kind is unsupported")
        service = _require_str(source, "service", max_len=128)
        account = _require_str(source, "account", max_len=256)
        out[name] = {
            "kind": kind,
            "service": service,
            "account": account,
        }
    return out


def _validate_http(d: dict) -> dict:
    method = d.get("method")
    _require(isinstance(method, str) and method, "http: 'method' required")
    method = method.upper()
    _require(
        method in IDEMPOTENT_HTTP_METHODS
        or method in ("POST", "PUT", "PATCH", "DELETE"),
        f"http: unsupported method {method!r}",
    )
    url_template = _require_str(d, "url_template")
    _require(
        url_template.startswith("https://"),
        "http: url_template must be https:// (no plaintext transport for secrets)",
    )

    headers = d.get("headers", {})
    _require(isinstance(headers, dict), "http: 'headers' must be an object")
    for k, v in headers.items():
        _require(
            isinstance(k, str) and isinstance(v, str),
            "http: header keys and values must be strings",
        )

    body = d.get("body", "")
    _require(isinstance(body, str), "http: 'body' must be a string")

    query = d.get("query", {})
    _require(isinstance(query, dict), "http: 'query' must be an object")
    for k, v in query.items():
        _require(
            isinstance(k, str) and isinstance(v, str),
            "http: query keys and values must be strings",
        )

    return {
        "method": method,
        "url_template": url_template,
        "headers": dict(headers),
        "body": body,
        "query": dict(query),
    }


def _validate_local_keychain(d: dict) -> dict:
    service = _require_str(d, "service", max_len=128)
    account = _require_str(d, "account", max_len=256)
    return {"service": service, "account": account}


def _validate_exec(d: dict) -> dict:
    argv = d.get("argv")
    _require(isinstance(argv, list) and argv, "exec: 'argv' must be a non-empty array")
    norm_argv = []
    for arg in argv:
        _require(isinstance(arg, str), "exec: argv entries must be strings")
        _require(
            not SECRET_PLACEHOLDER_RE.search(arg),
            "exec: secret placeholders are forbidden in argv; use stdin_template",
        )
        norm_argv.append(arg)

    binary = norm_argv[0]
    _require(os.path.isabs(binary), "exec: argv[0] must be an absolute path")
    _require(os.path.isfile(binary), "exec: argv[0] must be an existing binary")
    _require(os.access(binary, os.X_OK), "exec: argv[0] must be executable")

    stdin_template = d.get("stdin_template")
    _require(
        isinstance(stdin_template, str),
        "exec: 'stdin_template' must be a string containing a secret placeholder",
    )

    timeout_s = d.get("timeout_s", 30)
    _require(isinstance(timeout_s, int), "exec: 'timeout_s' must be an integer")
    _require(0 < timeout_s <= 300, "exec: 'timeout_s' must be between 1 and 300")

    return {
        "argv": norm_argv,
        "stdin_template": stdin_template,
        "timeout_s": timeout_s,
    }


_SINK_VALIDATORS = {
    "http": _validate_http,
    "local_keychain": _validate_local_keychain,
    "exec": _validate_exec,
}


def validate(descriptor: Any) -> dict:
    """Validate + normalize a provider descriptor. Raises DescriptorError.

    Returned shape (canonical):
        {
          "provider_id": str,
          "label": str,            # provider's free-text — UNTRUSTED, display-only
          "sink_kind": "http" | "local_keychain",
          "sink": { ...kind-specific... },
          "output_contract": "opaque" | "echo_safe",
        }

    ``output_contract`` is the provider's claim about its result; the broker
    still scans every result for the secret regardless (defense in depth).
    """
    _require(isinstance(descriptor, dict), "descriptor must be an object")

    provider_id = _require_str(descriptor, "provider_id", max_len=128)
    label = _require_str(descriptor, "label", max_len=512)

    sink_kind = descriptor.get("sink_kind")
    _require(
        sink_kind in SUPPORTED_SINK_KINDS,
        f"sink_kind must be one of {SUPPORTED_SINK_KINDS}, got {sink_kind!r}",
    )

    output_contract = descriptor.get("output_contract", "opaque")
    _require(
        output_contract in ("opaque", "echo_safe"),
        "output_contract must be 'opaque' or 'echo_safe'",
    )

    sink_raw = descriptor.get("sink")
    _require(isinstance(sink_raw, dict), "'sink' must be an object")
    sink = _SINK_VALIDATORS[sink_kind](sink_raw)
    secret_names = _secret_names_for_sink(sink_kind, sink)
    secret_sources = _validate_secret_sources(
        descriptor.get("secret_sources"),
        secret_names,
    )

    norm = {
        "provider_id": provider_id,
        "label": label,
        "sink_kind": sink_kind,
        "sink": sink,
        "secret_names": secret_names,
        "secret_sources": secret_sources,
        "output_contract": output_contract,
    }
    _require(secret_names, "descriptor must reference at least one secret")
    return norm


def _secret_names_for_sink(sink_kind: str, sink: dict) -> list[str]:
    names: set[str] = set()
    if sink_kind == "http":
        blobs = [sink["url_template"], sink["body"]]
        blobs += list(sink["headers"].values())
        blobs += list(sink["query"].values())
        for blob in blobs:
            names.update(extract_secret_names(blob))
    if sink_kind == "local_keychain":
        names.add(DEFAULT_SECRET_NAME)
    if sink_kind == "exec":
        names.update(extract_secret_names(sink["stdin_template"]))
    return sorted(names)


def extract_secret_names(template: str) -> set[str]:
    """Secret placeholders supported in templates:

    - {{secret}} maps to the default secret named "secret".
    - {{secret:name}} maps to a named secret.
    """
    return {
        match.group(1) or DEFAULT_SECRET_NAME
        for match in SECRET_PLACEHOLDER_RE.finditer(template)
    }


def substitute_secrets(template: str, secrets: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or DEFAULT_SECRET_NAME
        return secrets[name]

    return SECRET_PLACEHOLDER_RE.sub(_replace, template)


def coerce_secret_map(secret: str | dict[str, str]) -> dict[str, str]:
    if isinstance(secret, str):
        return {DEFAULT_SECRET_NAME: secret}
    return dict(secret)


def canonical_json(norm: dict) -> str:
    """Stable serialization for hashing — sorted keys, no whitespace."""
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def descriptor_hash(norm: dict) -> str:
    return hashlib.sha256(canonical_json(norm).encode("utf-8")).hexdigest()
