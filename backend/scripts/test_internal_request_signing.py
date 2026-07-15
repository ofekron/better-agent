"""HMAC request signing for /api/internal/*.

Locks:
- sign/verify roundtrip; wrong key, malformed headers, timestamp skew,
  and nonce replay are all rejected (fail closed)
- the runtime gate accepts a signed core request (404 past auth on an
  unknown internal route) and REJECTS the same secret as a raw bearer
- a replayed signed request is rejected by the gate's nonce guard
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-req-signing-")

import internal_request_auth as ira


def _cache() -> ira.NonceCache:
    return ira.NonceCache(ttl_seconds=600)


def test_sign_verify_roundtrip():
    headers = ira.sign("secret-key", "POST", "/api/internal/echo", b'{"a":1}')
    assert ira.verify(
        ["other", "secret-key"], "POST", "/api/internal/echo", b'{"a":1}',
        headers, nonce_cache=_cache(),
    )


def test_wrong_key_body_path_and_method_rejected():
    headers = ira.sign("secret-key", "POST", "/api/internal/echo", b"{}")
    for keys, method, path, body in (
        (["not-the-key"], "POST", "/api/internal/echo", b"{}"),
        (["secret-key"], "GET", "/api/internal/echo", b"{}"),
        (["secret-key"], "POST", "/api/internal/other", b"{}"),
        (["secret-key"], "POST", "/api/internal/echo", b'{"tampered":1}'),
    ):
        assert not ira.verify(keys, method, path, body, headers, nonce_cache=_cache())


def test_timestamp_skew_rejected():
    headers = ira.sign("k", "POST", "/x", b"")
    stale = int(headers[ira.HEADER_TIMESTAMP]) - (ira.DEFAULT_SKEW_SECONDS + 5)
    forged = dict(headers)
    forged[ira.HEADER_TIMESTAMP] = str(stale)
    # Re-sign honestly at the stale timestamp so only skew can reject it.
    import hashlib, hmac as hm
    canonical = ira.canonical_string("POST", "/x", b"", forged[ira.HEADER_NONCE], stale)
    forged[ira.HEADER_SIGNATURE] = "v1=" + hm.new(
        b"k", canonical.encode(), hashlib.sha256
    ).hexdigest()
    assert not ira.verify(["k"], "POST", "/x", b"", forged, nonce_cache=_cache())


def test_nonce_replay_rejected():
    cache = _cache()
    headers = ira.sign("k", "POST", "/x", b"")
    assert ira.verify(["k"], "POST", "/x", b"", headers, nonce_cache=cache)
    assert not ira.verify(["k"], "POST", "/x", b"", headers, nonce_cache=cache)


def test_gate_signed_core_passes_and_bearer_core_rejected():
    from fastapi.testclient import TestClient

    import main

    core_token = main.coordinator.internal_token
    assert core_token
    client = TestClient(main.app, raise_server_exceptions=False)
    path = "/api/internal/definitely-not-a-route"

    # Signed with the core secret: passes the gate, fails only at routing
    # (404/405 — never the gate's 403).
    signed = ira.sign(core_token, "POST", path, b"{}")
    response = client.post(path, content=b"{}", headers=signed)
    assert response.status_code in (404, 405), response.text

    # Replay of the exact same signed headers: nonce guard rejects.
    replay = client.post(path, content=b"{}", headers=signed)
    assert replay.status_code == 403, replay.text

    # The same secret as a raw bearer: rejected — core must sign.
    bearer = client.post(path, content=b"{}", headers={"X-Internal-Token": core_token})
    assert bearer.status_code == 403, bearer.text
    assert "signed" in bearer.json()["detail"]

    # Garbage signature: rejected.
    bad = dict(ira.sign(core_token, "POST", path, b"{}"))
    bad[ira.HEADER_SIGNATURE] = "v1=" + "0" * 64
    garbage = client.post(path, content=b"{}", headers=bad)
    assert garbage.status_code == 403, garbage.text


def test_gate_ambient_signed_request_classifies_ambient_principal():
    from fastapi.testclient import TestClient

    import ambient_principal
    import main

    token, principal = ambient_principal.registry.issue(
        extension_id="core",
        server_name="ui",
        permissions=["ui.open_file_panel"],
        os_user_id="test-user",
        source_kind="core",
        core_server="ui",
    )
    try:
        client = TestClient(main.app, raise_server_exceptions=False)
        path = "/api/internal/open-file-panel"
        body = b"{}"
        signed = ira.sign(token, "POST", path, body)
        response = client.post(path, content=body, headers=signed)
        # Past the gate (403 would mean signature/permission rejection);
        # the handler itself may 4xx on the empty payload but not 403.
        assert response.status_code != 403, response.text
    finally:
        ambient_principal.registry.revoke(principal.principal_id)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
