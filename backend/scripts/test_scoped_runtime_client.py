from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

import operation_authority
import operation_catalog
import scoped_runtime_client

pytestmark = pytest.mark.anyio


@dataclass
class _Policy:
    context_required: bool = False
    resource_fields: list[str] = field(default_factory=list)


@dataclass
class _Descriptor:
    policy: _Policy = field(default_factory=_Policy)


class _Response:
    def __init__(self, root) -> None:
        self.root = root


class _Client:
    def __init__(self, root="done") -> None:
        self.root = root
        self.calls: list[tuple] = []

    async def run(self, operation, payload):
        self.calls.append((operation, payload))
        return _Response(self.root)


class _Catalog:
    def __init__(self, *, generation="gen-1", client=None, descriptor=None) -> None:
        self.generation = generation
        self.client = client or _Client()
        self._descriptor = descriptor or _Descriptor()
        self.descriptor_calls: list[str] = []

    def descriptor(self, key):
        self.descriptor_calls.append(key)
        return self._descriptor


class _Principal:
    """Fake RuntimePrincipal backing the verified wrapper."""

    def __init__(self, *, allows=True, context_complete=True, permitted_resources=None):
        self._allows = allows
        self.context_complete = context_complete
        self.permitted_resources = set(permitted_resources or [])

    def allows(self, operation):
        return self._allows


class _Verified:
    """Stands in for operation_authority.VerifiedPrincipal after verify()."""

    def __init__(self, principal):
        self.principal = principal


def _client(monkeypatch, *, principal=None, catalog=None):
    principal = principal or _Principal()
    bound: list = []
    monkeypatch.setattr(
        scoped_runtime_client.operation_authority,
        "verify",
        lambda p: _Verified(principal),
    )

    @contextmanager
    def _bind(p):
        bound.append(p)
        yield

    monkeypatch.setattr(scoped_runtime_client.operation_authority, "bind", _bind)
    client = scoped_runtime_client.ScopedRuntimeClient(_Verified(principal), catalog or _Catalog())
    return client, principal, bound


class TestProperties:
    def test_principal_property_returns_wrapped_principal(self, monkeypatch) -> None:
        principal = _Principal()
        client, _, _ = _client(monkeypatch, principal=principal)
        assert client.principal is principal

    def test_verified_principal_property_returns_constructor_arg(self, monkeypatch) -> None:
        client, _, _ = _client(monkeypatch)
        assert isinstance(client.verified_principal, _Verified)

    def test_execution_generation_from_catalog(self, monkeypatch) -> None:
        catalog = _Catalog(generation="gen-42")
        client, _, _ = _client(monkeypatch, catalog=catalog)
        assert client.execution_generation == "gen-42"

    def test_constructor_defaults_to_current_catalog(self, monkeypatch) -> None:
        # __init__ does not call verify/bind; passing catalog=None routes to
        # operation_catalog.current().
        current = _Catalog(generation="current-gen")
        monkeypatch.setattr(operation_catalog, "current", lambda: current)
        client = scoped_runtime_client.ScopedRuntimeClient(_Verified(_Principal()))
        assert client.execution_generation == "current-gen"


class TestInvoke:
    async def test_authorized_invokes_and_returns_root(self, monkeypatch) -> None:
        catalog = _Catalog(client=_Client(root={"ok": 1}))
        client, principal, bound = _client(monkeypatch, catalog=catalog)
        result = await client.invoke("op.do", {"a": 1})
        assert result == {"ok": 1}
        assert catalog.descriptor_calls == ["op.do"]
        assert catalog.client.calls == [("op.do", {"a": 1})]
        assert bound == [principal]  # run happened inside operation_authority.bind

    async def test_not_authorized_raises_before_invoke(self, monkeypatch) -> None:
        catalog = _Catalog()
        client, _, _ = _client(monkeypatch, principal=_Principal(allows=False), catalog=catalog)
        with pytest.raises(PermissionError) as exc:
            await client.invoke("op.do", {})
        assert "not authorized for op.do" in str(exc.value)
        assert catalog.client.calls == []  # never reached the handler

    async def test_context_required_but_incomplete_raises(self, monkeypatch) -> None:
        catalog = _Catalog(descriptor=_Descriptor(policy=_Policy(context_required=True)))
        client, _, _ = _client(
            monkeypatch, principal=_Principal(context_complete=False), catalog=catalog
        )
        with pytest.raises(PermissionError) as exc:
            await client.invoke("op.do", {})
        assert "fully bound runtime context" in str(exc.value)
        assert "op.do" in str(exc.value)
        assert catalog.client.calls == []

    async def test_context_required_and_complete_proceeds(self, monkeypatch) -> None:
        catalog = _Catalog(descriptor=_Descriptor(policy=_Policy(context_required=True)))
        client, _, _ = _client(
            monkeypatch, principal=_Principal(context_complete=True), catalog=catalog
        )
        assert await client.invoke("op.do", {}) == "done"

    async def test_resource_field_permitted_proceeds(self, monkeypatch) -> None:
        catalog = _Catalog(
            descriptor=_Descriptor(policy=_Policy(resource_fields=["file"]))
        )
        client, _, _ = _client(
            monkeypatch,
            principal=_Principal(permitted_resources={"file:/safe"}),
            catalog=catalog,
        )
        assert await client.invoke("op.do", {"file": "/safe"}) == "done"

    async def test_resource_field_not_permitted_raises(self, monkeypatch) -> None:
        catalog = _Catalog(
            descriptor=_Descriptor(policy=_Policy(resource_fields=["file"]))
        )
        client, _, _ = _client(
            monkeypatch,
            principal=_Principal(permitted_resources={"file:/other"}),
            catalog=catalog,
        )
        with pytest.raises(PermissionError) as exc:
            await client.invoke("op.do", {"file": "/forbidden"})
        assert "not authorized for file" in str(exc.value)
        assert catalog.client.calls == []

    async def test_resource_field_missing_in_payload_raises(self, monkeypatch) -> None:
        # payload lacks the field → value None → "field:None" not in permitted set
        catalog = _Catalog(
            descriptor=_Descriptor(policy=_Policy(resource_fields=["file"]))
        )
        client, _, _ = _client(
            monkeypatch, principal=_Principal(permitted_resources=set()), catalog=catalog
        )
        with pytest.raises(PermissionError):
            await client.invoke("op.do", {})
        assert catalog.client.calls == []

    async def test_multiple_resource_fields_all_must_pass(self, monkeypatch) -> None:
        catalog = _Catalog(
            descriptor=_Descriptor(policy=_Policy(resource_fields=["a", "b"]))
        )
        client, _, _ = _client(
            monkeypatch,
            principal=_Principal(permitted_resources={"a:1", "b:2"}),
            catalog=catalog,
        )
        assert await client.invoke("op.do", {"a": "1", "b": "2"}) == "done"
