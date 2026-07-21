#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_HOME = tempfile.mkdtemp(prefix="better-agent-provider-transport-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME

import provider_transport
import provider
from provider_manifest import SPECS


PEM = "-----BEGIN CERTIFICATE-----\nVEVTVA==\n-----END CERTIFICATE-----\n"


def response(**overrides):
    payload = {
        "version": 1,
        "enabled": True,
        "forward_proxy_url": "http://127.0.0.1:18888",
        "gateway_base_url": "http://127.0.0.1:18889/route-token",
        "ca_certificate_pem": PEM,
        "ca_sha256": hashlib.sha256(PEM.encode("ascii")).hexdigest(),
    }
    payload.update(overrides)
    return 200, json.dumps(payload).encode()


def apply(kind="claude"):
    with (
        patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[("x", "/transport")]),
        patch.object(provider_transport, "invoke_extension_backend_sync", return_value=response()),
    ):
        return provider_transport.apply_provider_transport(
            {"NO_PROXY": "internal.test"},
            provider_id="provider-1",
            provider_kind=kind,
            provider_mode="subscription",
        )


def test_claude_gateway_and_forward_proxy():
    env = apply()
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18889/route-token"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:18888"
    assert env["SSL_CERT_FILE"].startswith(TEST_HOME)
    assert env["NO_PROXY"] == "internal.test,127.0.0.1,localhost,::1"


def test_non_gateway_provider_uses_forward_proxy_only():
    env = apply("gemini")
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["https_proxy"] == "http://127.0.0.1:18888"


def test_rejects_non_loopback_proxy():
    with (
        patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[("x", "/transport")]),
        patch.object(provider_transport, "invoke_extension_backend_sync", return_value=response(forward_proxy_url="http://example.com:8080")),
    ):
        try:
            provider_transport.apply_provider_transport({}, provider_id="p", provider_kind="gemini", provider_mode="")
        except provider_transport.ProviderTransportError:
            return
    raise AssertionError("non-loopback proxy was accepted")


def test_rejects_multiple_hooks():
    with patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[("a", "/x"), ("b", "/y")]):
        try:
            provider_transport.apply_provider_transport({}, provider_id="p", provider_kind="gemini", provider_mode="")
        except provider_transport.ProviderTransportError:
            return
    raise AssertionError("multiple provider transport hooks were accepted")


def test_no_hook_preserves_environment():
    original = {"HTTPS_PROXY": "http://existing.test:8080"}
    with patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[]):
        result = provider_transport.apply_provider_transport(
            original, provider_id="p", provider_kind="claude", provider_mode="subscription"
        )
    assert result is original


def test_active_hook_failure_is_strict():
    with (
        patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[("x", "/transport")]),
        patch.object(provider_transport, "invoke_extension_backend_sync", return_value=(503, b"")),
    ):
        try:
            provider_transport.apply_provider_transport(
                {}, provider_id="p", provider_kind="claude", provider_mode="subscription"
            )
        except provider_transport.ProviderTransportError:
            return
    raise AssertionError("active transport hook failed open")


def test_each_local_provider_executes_transport_finalizer():
    with (
        patch.object(provider.Provider, "require_runtime_credential", return_value=None),
        patch.object(provider_transport.extension_store, "provider_transport_hooks", return_value=[("x", "/transport")]),
        patch.object(provider_transport, "invoke_extension_backend_sync", return_value=response()),
    ):
        for spec in SPECS.values():
            if spec.virtual:
                continue
            provider_class = provider._resolve_class(spec.kind)
            instance = object.__new__(provider_class)
            instance.id = f"test-{spec.kind}"
            instance._record = {
                "id": instance.id,
                "kind": spec.kind,
                "mode": "subscription",
                "config_dir": "",
                "api_key": "",
                "base_url": "",
            }
            env = instance.build_env()
            assert env["HTTPS_PROXY"] == "http://127.0.0.1:18888", spec.kind
            assert env["SSL_CERT_FILE"].startswith(TEST_HOME), spec.kind
            if spec.transport_gateway_env:
                assert env[spec.transport_gateway_env].startswith("http://127.0.0.1:18889/"), spec.kind


if __name__ == "__main__":
    test_claude_gateway_and_forward_proxy()
    test_non_gateway_provider_uses_forward_proxy_only()
    test_rejects_non_loopback_proxy()
    test_rejects_multiple_hooks()
    test_no_hook_preserves_environment()
    test_active_hook_failure_is_strict()
    test_each_local_provider_executes_transport_finalizer()
    print("provider transport tests passed")
