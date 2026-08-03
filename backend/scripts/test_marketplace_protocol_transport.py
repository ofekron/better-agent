"""Unit coverage for marketplace_protocol_transport.py.

This module is the HTTP transport over the Marketplace protocol. It talks to
an EXTERNAL server (the ofek-dev.com marketplace), not to the Better Agent
backend process, so there is no real-backend path that exercises it without
hitting a live third party. The deterministic surface — identifier/path
validators, request/response shape checks, origin configuration, and the
urllib response-handling branches — is therefore unit-tier: the urllib I/O
seam is mocked so every branch asserts on parsed/validated behavior, not on
network reachability.
"""
from __future__ import annotations

import json
import urllib.error

import pytest
from fastapi import HTTPException

import marketplace_protocol_transport as mpt


_HEX32 = "a" * 32
_DEVICE_ID = "badvc_" + _HEX32
_ACTION_ID = "baact_" + _HEX32
_SHA256 = "a" * 64
_CHALLENGE = "bachal_" + "a" * 43
_SIGNATURE = "A" * 86  # [A-Za-z0-9_-]{86}
_FUTURE = "2099-01-01T00:00:00+00:00"
_VALID_ORIGIN = "https://market.example.test/api/marketplace"


@pytest.fixture(autouse=True)
def _fixed_origin(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_MARKETPLACE_BASE_URL", _VALID_ORIGIN)


# ── scalar validators ────────────────────────────────────────────


def test_is_string():
    assert mpt._is_string("x") is True
    assert mpt._is_string(1) is False


@pytest.mark.parametrize("value", ["x", " "])
def test_is_non_empty_string_truthy(value):
    assert mpt._is_non_empty_string(value) is True


@pytest.mark.parametrize("value", ["", None, 1, []])
def test_is_non_empty_string_falsy(value):
    assert mpt._is_non_empty_string(value) is False


def test_is_revision_accepts_non_negative_int():
    assert mpt._is_revision(0) is True
    assert mpt._is_revision(7) is True


@pytest.mark.parametrize("value", [-1, 1.0, True, False, "1", None])
def test_is_revision_rejects(value):
    assert mpt._is_revision(value) is False


# ── _is_challenge_batch ──────────────────────────────────────────


def test_challenge_batch_valid():
    assert mpt._is_challenge_batch([_CHALLENGE]) is True
    assert mpt._is_challenge_batch([_CHALLENGE] * 4) is True


@pytest.mark.parametrize(
    "value",
    [
        "not-a-list",
        [],
        [_CHALLENGE] * 5,  # over challenge_batch_max (4)
        ["bad"],
        [_CHALLENGE, 1],
        [_CHALLENGE.replace("a", "!")],
    ],
)
def test_challenge_batch_rejects(value):
    assert mpt._is_challenge_batch(value) is False


# ── _is_leased_action ────────────────────────────────────────────


def test_leased_action_none_is_allowed():
    assert mpt._is_leased_action(None) is True


def test_leased_action_valid_dict_passes():
    leased = {
        "action_id": _ACTION_ID,
        "action_type": "enable",
        "lease_capability": "primary",
        "lease_expires_at": _FUTURE,
        "target_version": "",
        "catalog_snapshot_sha256": _SHA256,
        "extension": {"id": "myext", "name": "My Ext"},
    }
    assert mpt._is_leased_action(leased) is True


def test_leased_action_invalid_returns_false():
    assert mpt._is_leased_action({"action_id": "nope"}) is False


# ── origin() ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "https://ofek-dev.com/api/marketplace"),  # default
        ("https://ofek-dev.com/api/marketplace/", "https://ofek-dev.com/api/marketplace"),  # trailing slash
        ("  https://ofek-dev.com/api/marketplace  ", "https://ofek-dev.com/api/marketplace"),  # whitespace
        ("  https://host.test/p/  ", "https://host.test/p"),  # override + strip + slash
    ],
)
def test_origin_valid(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("BETTER_AGENT_MARKETPLACE_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("BETTER_AGENT_MARKETPLACE_BASE_URL", raw)
    assert mpt.origin() == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://insecure.test",  # non-https
        "https:///no-host",  # no hostname
        "https://user:pw@host.test",  # userinfo
        "https://host.test?q=1",  # query
        "https://host.test#frag",  # fragment
        "https://host.test/%2",  # percent in path
        "https://host.test/a\\b",  # backslash
        "https://host.test/a//b",  # double slash
        "https://host.test/./a",  # dot segment
        "https://host.test/../a",  # parent segment
    ],
)
def test_origin_rejects_invalid_configuration(monkeypatch, url):
    monkeypatch.setenv("BETTER_AGENT_MARKETPLACE_BASE_URL", url)
    with pytest.raises(HTTPException) as exc:
        mpt.origin()
    assert exc.value.status_code == 500
    assert exc.value.detail == "Marketplace server configuration is invalid"


# ── _operation_path ──────────────────────────────────────────────


def test_operation_path_builds_and_quotes_no_params():
    method, path, request_fields = mpt._operation_path("pair_context", {})
    assert method == "POST"
    assert path == "/protocol/v1/pair/context"
    assert request_fields == {"pair_token", "protocol_hash"}


def test_operation_path_substitutes_device_id():
    method, path, request_fields = mpt._operation_path(
        "device_challenges", {"device_id": _DEVICE_ID}
    )
    assert method == "POST"
    assert path == f"/protocol/v1/devices/{_DEVICE_ID}/challenges"
    assert request_fields == {"protocol_hash"}


def test_operation_path_substitutes_multiple_params():
    method, path, _ = mpt._operation_path(
        "fence", {"device_id": _DEVICE_ID, "action_id": _ACTION_ID}
    )
    assert method == "POST"
    assert path == f"/protocol/v1/devices/{_DEVICE_ID}/actions/{_ACTION_ID}/fence"


def test_operation_path_quotes_value():
    # device id must match its pattern, but quoting still applies to the raw
    # substitution; use a value containing a pattern-safe char to confirm it
    # flows through quote() unchanged for a valid identifier.
    _, path, _ = mpt._operation_path("revoke", {"device_id": _DEVICE_ID})
    assert _DEVICE_ID in path


def test_operation_path_unknown_operation():
    with pytest.raises(HTTPException) as exc:
        mpt._operation_path("bogus", {})
    assert exc.value.status_code == 500
    assert exc.value.detail == "Marketplace operation is invalid"


def test_operation_path_param_mismatch():
    with pytest.raises(HTTPException) as exc:
        mpt._operation_path("device_challenges", {})  # missing device_id
    assert exc.value.status_code == 500
    assert exc.value.detail == "Marketplace path is incomplete"


def test_operation_path_unknown_parameter_name():
    with pytest.raises(HTTPException) as exc:
        mpt._operation_path("revoke", {"not_a_param": _DEVICE_ID})
    assert exc.value.status_code == 500


@pytest.mark.parametrize(
    "operation,name,value",
    [
        ("revoke", "device_id", "not-a-device"),
        ("action_metadata", "action_id", "badact_xyz"),
    ],
)
def test_operation_path_rejects_invalid_identifier(operation, name, value):
    with pytest.raises(HTTPException) as exc:
        mpt._operation_path(operation, {name: value})
    assert exc.value.status_code == 400
    assert exc.value.detail == "Marketplace identifier is invalid"


# ── request() pre-call validation ────────────────────────────────


def test_request_rejects_body_shape_mismatch():
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "pair_context",
            {},
            access_token="tok",
            body={"unexpected": "1"},
        )
    assert exc.value.status_code == 400
    assert "invalid shape" in exc.value.detail


def test_request_unsigned_op_rejects_extraneous_proof_fields():
    # pair_context is NOT signed; proof fields aren't part of its request shape,
    # so the shape guard (not the proof-shape invariant) rejects them. The
    # proof-shape guard itself is an unreachable defensive invariant — see the
    # pragma in request(): the shape check guarantees set(body)==request_fields,
    # and signed ops always carry challenge+signature while unsigned never do.
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "pair_context",
            {},
            access_token="tok",
            body={"pair_token": "t", "protocol_hash": "p", "challenge": "c", "signature": "s"},
        )
    assert exc.value.status_code == 400
    assert "invalid shape" in exc.value.detail


def test_request_signed_op_rejects_missing_proof_fields():
    # revoke IS signed; dropping challenge/signature makes the body mismatch the
    # request shape, so the shape guard rejects it (proof-shape guard unreachable).
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "revoke",
            {"device_id": _DEVICE_ID},
            access_token="tok",
            body={"protocol_hash": "p"},  # missing challenge/signature
        )
    assert exc.value.status_code == 400
    assert "invalid shape" in exc.value.detail


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "x" * 8193,
        "has space",
        "tab\there",
        "ctrl\x7f",
        "ctrl\x00",
    ],
)
def test_request_rejects_invalid_access_token(token):
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "pair_context",
            {},
            access_token=token,
            body={"pair_token": "t", "protocol_hash": "p"},
        )
    assert exc.value.status_code == 401
    assert exc.value.detail == "Marketplace login required"


# ── request() post-call validation via _request_sync seam ────────


def _stub_sync(monkeypatch, returns=None, raises=None):
    captured = {}

    def fake(method, path, body, access_token, signed):
        captured.update(
            method=method, path=path, body=dict(body), access_token=access_token, signed=signed
        )
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(mpt, "_request_sync", fake)
    return captured


def test_request_success_returns_validated_result(monkeypatch):
    result = {
        "site_label": "site",
        "account_label": "acct",
        "expires_at": _FUTURE,
        "catalog_snapshot_sha256": _SHA256,
    }
    captured = _stub_sync(monkeypatch, returns=result)
    out = mpt.request(
        "pair_context",
        {},
        access_token="tok",
        body={"pair_token": "t", "protocol_hash": "p"},
    )
    assert out == result
    assert captured["signed"] is False
    assert captured["method"] == "POST"


def test_request_signed_routes_to_sync_with_signed_flag(monkeypatch):
    captured = _stub_sync(
        monkeypatch,
        returns={"device_id": _DEVICE_ID, "revoked": True},
    )
    mpt.request(
        "revoke",
        {"device_id": _DEVICE_ID},
        access_token="tok",
        body={"protocol_hash": "p", "challenge": _CHALLENGE, "signature": _SIGNATURE},
    )
    assert captured["signed"] is True
    # request() passes the full body to _request_sync; the proof fields are
    # popped into headers INSIDE _request_sync (covered by the sync-level tests).
    assert captured["body"]["challenge"] == _CHALLENGE
    assert captured["body"]["signature"] == _SIGNATURE


def test_request_rejects_response_shape_mismatch(monkeypatch):
    _stub_sync(monkeypatch, returns={"site_label": "only"})
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "pair_context",
            {},
            access_token="tok",
            body={"pair_token": "t", "protocol_hash": "p"},
        )
    assert exc.value.status_code == 502
    assert "response has an invalid shape" in exc.value.detail


def test_request_rejects_response_with_invalid_values(monkeypatch):
    _stub_sync(
        monkeypatch,
        returns={
            "site_label": "site",
            "account_label": "acct",
            "expires_at": "",  # invalid: non-empty required
            "catalog_snapshot_sha256": _SHA256,
        },
    )
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "pair_context",
            {},
            access_token="tok",
            body={"pair_token": "t", "protocol_hash": "p"},
        )
    assert exc.value.status_code == 502
    assert "invalid values" in exc.value.detail


def test_request_projection_requires_matching_revision(monkeypatch):
    _stub_sync(
        monkeypatch,
        returns={"revision": 5},  # server revision != requested
    )
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "projection",
            {"device_id": _DEVICE_ID},
            access_token="tok",
            body={
                "protocol_hash": "p",
                "revision": 3,
                "extensions": [],
                "challenge": _CHALLENGE,
                "signature": _SIGNATURE,
            },
        )
    assert exc.value.status_code == 502
    assert "invalid values" in exc.value.detail


def test_request_projection_accepts_matching_revision(monkeypatch):
    _stub_sync(monkeypatch, returns={"revision": 3})
    out = mpt.request(
        "projection",
        {"device_id": _DEVICE_ID},
        access_token="tok",
        body={
            "protocol_hash": "p",
            "revision": 3,
            "extensions": [],
            "challenge": _CHALLENGE,
            "signature": _SIGNATURE,
        },
    )
    assert out == {"revision": 3}


def test_request_lease_validates_leased_action_response(monkeypatch):
    leased = {
        "action_id": _ACTION_ID,
        "action_type": "enable",
        "lease_capability": "primary",
        "lease_expires_at": _FUTURE,
        "target_version": "",
        "catalog_snapshot_sha256": _SHA256,
        "extension": {"id": "myext", "name": "My Ext"},
    }
    _stub_sync(monkeypatch, returns={"action": leased})
    out = mpt.request(
        "lease",
        {"device_id": _DEVICE_ID},
        access_token="tok",
        body={
            "protocol_hash": "p",
            "wait_seconds": 0,
            "challenge": _CHALLENGE,
            "signature": _SIGNATURE,
        },
    )
    assert out == {"action": leased}


def test_request_lease_rejects_malformed_action(monkeypatch):
    _stub_sync(monkeypatch, returns={"action": {"action_id": "nope"}})
    with pytest.raises(HTTPException) as exc:
        mpt.request(
            "lease",
            {"device_id": _DEVICE_ID},
            access_token="tok",
            body={
                "protocol_hash": "p",
                "wait_seconds": 0,
                "challenge": _CHALLENGE,
                "signature": _SIGNATURE,
            },
        )
    assert exc.value.status_code == 502


# ── _request_sync urllib seam ────────────────────────────────────


class _FakeResponse:
    def __init__(self, url, body):
        self._url = url
        self._body = body

    def geturl(self):
        return self._url

    def read(self, _n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeOpener:
    def __init__(self, handler, response):
        self._handler = handler
        self._response = response
        self.last_request = None

    def open(self, request, timeout=None):
        self.last_request = request
        self.last_timeout = timeout
        return self._response


def _patch_opener(monkeypatch, response_body=b"{}", url=None, raises=None):
    target_url = url if url is not None else f"{_VALID_ORIGIN}/protocol/v1/pair/context"
    response = _FakeResponse(target_url, response_body)

    class _OpenerFactory:
        def __init__(self):
            self.opener = None

        def __call__(self, *_args, **_kwargs):
            self.opener = _FakeOpener(self, response)
            if raises is not None:
                # Defer the raise to .open() so the opener is still constructed.
                original_open = self.opener.open

                def _raising(req, timeout=None):
                    self.opener.last_request = req
                    self.opener.last_timeout = timeout
                    raise raises

                self.opener.open = _raising
            return self.opener

    factory = _OpenerFactory()
    monkeypatch.setattr(mpt.urllib.request, "build_opener", factory)
    return factory


def _valid_proof_body():
    return {"protocol_hash": "p", "challenge": _CHALLENGE, "signature": _SIGNATURE}


def test_request_sync_unsigned_success_parses_json(monkeypatch):
    body = b'{"outcome":"ok"}'
    factory = _patch_opener(monkeypatch, body)
    result = mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert result == {"outcome": "ok"}
    # authorization + json headers wired through
    req = factory.opener.last_request
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Content-type") == "application/json"
    assert factory.opener.last_timeout == 15


def test_request_sync_lease_path_uses_longer_timeout(monkeypatch):
    factory = _patch_opener(
        monkeypatch,
        b'{"outcome":"ok"}',
        url=f"{_VALID_ORIGIN}/protocol/v1/devices/{_DEVICE_ID}/actions/lease",
    )
    mpt._request_sync(
        "POST", f"/protocol/v1/devices/{_DEVICE_ID}/actions/lease", {}, "tok", signed=False
    )
    assert factory.opener.last_timeout == 40


def test_request_sync_empty_body_returns_empty_dict(monkeypatch):
    _patch_opener(monkeypatch, b"")
    assert mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False) == {}


def test_request_sync_signed_moves_proof_to_headers(monkeypatch):
    factory = _patch_opener(
        monkeypatch,
        b'{"outcome":"ok"}',
    )
    mpt._request_sync(
        "POST",
        "/protocol/v1/pair/context",
        _valid_proof_body(),
        "tok",
        signed=True,
    )
    req = factory.opener.last_request
    # urllib stores each header via str.capitalize() on the whole name, so the
    # canonical stored key is "X-ba-device-challenge".
    assert req.headers["X-ba-device-challenge"] == _CHALLENGE
    assert req.headers["X-ba-device-signature"] == _SIGNATURE
    # proof fields removed from the JSON payload
    assert b"challenge" not in req.data
    assert b"signature" not in req.data


@pytest.mark.parametrize(
    "challenge,signature",
    [
        ("not-a-challenge", _SIGNATURE),
        (_CHALLENGE, "too-short"),
        (123, _SIGNATURE),
        (_CHALLENGE, None),
    ],
)
def test_request_sync_signed_rejects_invalid_proof(monkeypatch, challenge, signature):
    # Raises BEFORE any network call — no opener patch needed.
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync(
            "POST",
            "/protocol/v1/pair/context",
            {"protocol_hash": "p", "challenge": challenge, "signature": signature},
            "tok",
            signed=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Marketplace device proof is invalid"


def test_request_sync_refuses_redirect(monkeypatch):
    _patch_opener(monkeypatch, b"x", url="https://attacker.test/elsewhere")
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert "redirect" in exc.value.detail


def test_request_sync_rejects_oversized_body(monkeypatch):
    _patch_opener(monkeypatch, b"x" * (mpt._MAX_RESPONSE_BYTES + 1))
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert "too large" in exc.value.detail


def test_request_sync_rejects_invalid_json(monkeypatch):
    _patch_opener(monkeypatch, b"\xff\xfe not json")
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert "invalid JSON" in exc.value.detail


def test_request_sync_rejects_non_object_json(monkeypatch):
    _patch_opener(monkeypatch, b"[1,2,3]")
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert "invalid response" in exc.value.detail


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 410, 429])
def test_request_sync_maps_known_http_error_codes(monkeypatch, code):
    _patch_opener(monkeypatch, raises=urllib.error.HTTPError(
        "u", code, "err", {}, None
    ))
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == code
    assert exc.value.detail == "Marketplace protocol request failed"


def test_request_sync_maps_unknown_http_error_to_502(monkeypatch):
    _patch_opener(monkeypatch, raises=urllib.error.HTTPError(
        "u", 500, "err", {}, None
    ))
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502


def test_request_sync_maps_url_error_to_502(monkeypatch):
    _patch_opener(monkeypatch, raises=urllib.error.URLError("down"))
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert exc.value.detail == "Marketplace protocol unavailable"


def test_request_sync_maps_timeout_to_502(monkeypatch):
    _patch_opener(monkeypatch, raises=TimeoutError())
    with pytest.raises(HTTPException) as exc:
        mpt._request_sync("POST", "/protocol/v1/pair/context", {}, "tok", signed=False)
    assert exc.value.status_code == 502
    assert exc.value.detail == "Marketplace protocol unavailable"


def test_request_sync_payload_is_canonical_json(monkeypatch):
    factory = _patch_opener(monkeypatch, b'{"outcome":"ok"}')
    mpt._request_sync(
        "POST",
        "/protocol/v1/pair/context",
        {"b": 1, "a": 2},
        "tok",
        signed=False,
    )
    assert factory.opener.last_request.data == json.dumps(
        {"a": 2, "b": 1}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# ── module integrity ─────────────────────────────────────────────


def test_no_redirect_handler_refuses_redirects():
    # _NoRedirect is wired into build_opener to keep urllib from silently
    # following 3xx responses; the contract is "always decline".
    handler = mpt._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test") is None


def test_response_validators_cover_every_http_operation():
    # The import-time RuntimeError guard guarantees this invariant; assert it
    # holds against the shipped protocol so the guard is documented as a
    # static, unreachable-by-construction check (excluded via pragma).
    import marketplace_protocol as mp

    assert set(mpt._RESPONSE_VALUE_VALIDATORS) == set(mp.PROTOCOL["http"])
