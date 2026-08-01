"""Unit coverage for provider_runtime_hydration.

Pure frozen-dataclass validation: secret-hydration references, MCP prewarm
connection endpoints, the aggregate runtime-hydration bundle, and the
spawn-capability hydration step. The PreparedRuntimeCapabilities collaborator
is constructed directly via its frozen JSON-string fields so this test stays
decoupled from the unrelated plan-normalization in its ``create()`` path.
"""
from __future__ import annotations

import json
from types import MappingProxyType

import pytest
from codex_execution_common import ExecutionContractError
from provider_runtime_capability_model import PreparedRuntimeCapabilities

import provider_runtime_hydration as h

_SAFE_KIND = "kv.token"
_SAFE_VALUE = "ref-value"


def _ref(kind: str = _SAFE_KIND, value: str = _SAFE_VALUE) -> h.SecretHydrationRef:
    return h.SecretHydrationRef(kind=kind, value=value)


def _conn(endpoint: str = "tcp://127.0.0.1:7000", secret: str = "s") -> h.PrewarmConnectionHydration:
    return h.PrewarmConnectionHydration(endpoint=endpoint, connect_secret=secret)


def _prepared(extension_ids: list[str], prewarm: dict) -> PreparedRuntimeCapabilities:
    return PreparedRuntimeCapabilities(
        _manifest_json=json.dumps({"extension_ids": extension_ids}),
        payload=b"",
        _plan_json=json.dumps({"tools": []}),
        _prewarm_json=json.dumps(prewarm),
    )


def _refs(
    *,
    provider_identity=None,
    extension_identities=None,
    runtime_broker=None,
    backend_transport=None,
    prewarm_connections=None,
) -> h.RuntimeHydrationRefs:
    return h.RuntimeHydrationRefs(
        provider_identity=provider_identity,
        extension_identities={} if extension_identities is None else extension_identities,
        runtime_broker=runtime_broker,
        backend_transport=backend_transport,
        prewarm_connections={} if prewarm_connections is None else prewarm_connections,
    )


# --- SecretHydrationRef ----------------------------------------------------


def test_secret_ref_valid() -> None:
    ref = _ref()
    assert ref.kind == _SAFE_KIND
    assert ref.value == _SAFE_VALUE


def test_secret_ref_repr_hides_value() -> None:
    assert _SAFE_VALUE not in repr(_ref())


@pytest.mark.parametrize(
    "kind, value",
    [
        (5, _SAFE_VALUE),          # kind not str
        ("", _SAFE_VALUE),          # kind regex mismatch (empty)
        ("bad kind", _SAFE_VALUE),  # kind regex mismatch (space)
        ("k" * 129, _SAFE_VALUE),   # kind too long
        (_SAFE_KIND, 5),            # value not str
        (_SAFE_KIND, ""),           # value empty
        (_SAFE_KIND, "v" * 16385),  # value too long
    ],
)
def test_secret_ref_invalid(kind, value) -> None:
    with pytest.raises(ExecutionContractError, match="invalid secret hydration reference"):
        h.SecretHydrationRef(kind=kind, value=value)


# --- PrewarmConnectionHydration -------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:7000",
        "tcp://localhost:7000",
        "tcp://[::1]:7000",
    ],
)
def test_prewarm_connection_valid(endpoint: str) -> None:
    conn = _conn(endpoint=endpoint)
    assert conn.endpoint == endpoint


def test_prewarm_connection_repr_hides_secret() -> None:
    assert "s" not in repr(_conn(secret="hidden-secret"))


@pytest.mark.parametrize(
    "endpoint, secret",
    [
        (5, "s"),                      # endpoint not str
        ("", "s"),                      # endpoint empty
        ("x" * 4097, "s"),              # endpoint too long
        ("tcp://127.0.0.1:7000", 5),   # secret not str
        ("tcp://127.0.0.1:7000", ""),   # secret empty
        ("tcp://127.0.0.1:7000", "s" * 4097),  # secret too long
    ],
)
def test_prewarm_connection_field_invalid(endpoint, secret) -> None:
    with pytest.raises(ExecutionContractError, match="invalid MCP prewarm hydration"):
        h.PrewarmConnectionHydration(endpoint=endpoint, connect_secret=secret)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:7000",          # wrong scheme
        "tcp://example.com:7000",         # non-local host
        "tcp://127.0.0.1",                # no port
        "tcp://u@127.0.0.1:7000",         # username present
        "tcp://127.0.0.1:7000?x=1",       # query present
        "tcp://127.0.0.1:7000#frag",      # fragment present
    ],
)
def test_prewarm_connection_endpoint_invalid(endpoint: str) -> None:
    with pytest.raises(ExecutionContractError, match="invalid MCP prewarm endpoint"):
        _conn(endpoint=endpoint)


# --- RuntimeHydrationRefs --------------------------------------------------


def test_refs_minimal_valid() -> None:
    refs = _refs()
    assert refs.provider_identity is None
    assert refs.extension_identities == {}
    assert refs.prewarm_connections == {}


def test_refs_fully_populated_valid() -> None:
    refs = _refs(
        provider_identity=_ref(),
        extension_identities={"ext.one": _ref()},
        runtime_broker=_ref(),
        backend_transport=_ref(),
        prewarm_connections={"conn": _conn()},
    )
    assert refs.provider_identity.kind == _SAFE_KIND
    assert set(refs.extension_identities) == {"ext.one"}
    assert set(refs.prewarm_connections) == {"conn"}


def test_refs_mappings_are_immutable_proxies() -> None:
    refs = _refs(extension_identities={"ext": _ref()}, prewarm_connections={"c": _conn()})
    assert isinstance(refs.extension_identities, MappingProxyType)
    assert isinstance(refs.prewarm_connections, MappingProxyType)
    with pytest.raises(TypeError):
        refs.extension_identities["ext"] = _ref()
    with pytest.raises(TypeError):
        refs.prewarm_connections["c"] = _conn()


@pytest.mark.parametrize(
    "field, bad",
    [
        ("provider_identity", "not-a-ref"),
        ("runtime_broker", "not-a-ref"),
        ("backend_transport", "not-a-ref"),
    ],
)
def test_refs_single_ref_fields_invalid(field: str, bad) -> None:
    with pytest.raises(ExecutionContractError):
        _refs(**{field: bad})


@pytest.mark.parametrize(
    "mapping",
    [
        "extension_identities",
        "prewarm_connections",
    ],
)
def test_refs_mapping_not_dict(mapping: str) -> None:
    with pytest.raises(ExecutionContractError):
        _refs(**{mapping: MappingProxyType({})})


def test_refs_extension_identity_key_not_str() -> None:
    with pytest.raises(ExecutionContractError, match="extension identity hydration"):
        _refs(extension_identities={5: _ref()})


def test_refs_extension_identity_key_regex_mismatch() -> None:
    with pytest.raises(ExecutionContractError, match="extension identity hydration"):
        _refs(extension_identities={"bad key": _ref()})


def test_refs_extension_identity_value_wrong_type() -> None:
    with pytest.raises(ExecutionContractError, match="extension identity hydration"):
        _refs(extension_identities={"ext": "not-a-ref"})


def test_refs_prewarm_connection_key_regex_mismatch() -> None:
    with pytest.raises(ExecutionContractError, match="prewarm connection hydration"):
        _refs(prewarm_connections={"bad key": _conn()})


def test_refs_prewarm_connection_value_wrong_type() -> None:
    with pytest.raises(ExecutionContractError, match="prewarm connection hydration"):
        _refs(prewarm_connections={"conn": "not-a-conn"})


# --- hydrate_spawn_capabilities -------------------------------------------


def test_hydrate_rejects_non_prepared() -> None:
    with pytest.raises(ExecutionContractError, match="invalid prepared runtime capabilities"):
        h.hydrate_spawn_capabilities({"not": "prepared"}, _refs())  # type: ignore[arg-type]


def test_hydrate_rejects_unknown_extension_identity() -> None:
    prepared = _prepared(["ext.one"], {})
    refs = _refs(extension_identities={"ext.two": _ref()})
    with pytest.raises(ExecutionContractError, match="unknown extension identity hydration"):
        h.hydrate_spawn_capabilities(prepared, refs)


def test_hydrate_rejects_extra_prewarm_connection() -> None:
    # Plan has no ready connection, but hydration supplies one.
    prepared = _prepared([], {})
    refs = _refs(prewarm_connections={"conn": _conn()})
    with pytest.raises(ExecutionContractError, match="MCP prewarm hydration does not match plan"):
        h.hydrate_spawn_capabilities(prepared, refs)


def test_hydrate_rejects_missing_prewarm_connection() -> None:
    # Plan requires a ready connection, but hydration omits it.
    prepared = _prepared([], {"conn": {"status": "ready"}})
    with pytest.raises(ExecutionContractError, match="MCP prewarm hydration does not match plan"):
        h.hydrate_spawn_capabilities(prepared, _refs())


def test_hydrate_rejects_wrong_prewarm_connection_name() -> None:
    prepared = _prepared([], {"conn": {"status": "ready"}})
    refs = _refs(prewarm_connections={"other": _conn()})
    with pytest.raises(ExecutionContractError, match="MCP prewarm hydration does not match plan"):
        h.hydrate_spawn_capabilities(prepared, refs)


def test_hydrate_happy_path_no_connections() -> None:
    prepared = _prepared(["ext.one"], {"conn": {"status": "pending"}})
    refs = _refs(extension_identities={"ext.one": _ref()})
    out = h.hydrate_spawn_capabilities(prepared, refs)
    assert isinstance(out, h.HydratedSpawnCapabilities)
    assert out.plan == {"tools": []}
    assert out.prewarm_status == {"conn": {"status": "pending"}}
    assert out.hydration is refs


def test_hydrate_happy_path_with_ready_connection() -> None:
    prepared = _prepared(["ext.one"], {"conn": {"status": "ready"}})
    refs = _refs(
        extension_identities={"ext.one": _ref()},
        prewarm_connections={"conn": _conn()},
    )
    out = h.hydrate_spawn_capabilities(prepared, refs)
    assert isinstance(out, h.HydratedSpawnCapabilities)
    assert set(out.hydration.prewarm_connections) == {"conn"}
