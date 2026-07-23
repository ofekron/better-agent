from __future__ import annotations

from typing import Any

import operation_authority
import operation_catalog


class ScopedRuntimeClient:
    def __init__(
        self,
        principal: operation_authority.VerifiedPrincipal,
        catalog: operation_catalog.PublishedCatalog | None = None,
    ) -> None:
        self._principal = principal
        self._catalog = catalog or operation_catalog.current()

    @property
    def principal(self):
        return self._principal.principal

    @property
    def verified_principal(self) -> operation_authority.VerifiedPrincipal:
        return self._principal

    @property
    def execution_generation(self) -> str:
        return self._catalog.generation

    async def invoke(self, operation: str, payload: dict[str, Any]) -> Any:
        descriptor = self._catalog.descriptor(operation)
        verified = operation_authority.verify(self._principal)
        principal = verified.principal
        if not principal.allows(operation):
            raise PermissionError(f"principal is not authorized for {operation}")
        if descriptor.policy.context_required and not principal.context_complete:
            raise PermissionError(f"operation requires a fully bound runtime context: {operation}")
        for field in descriptor.policy.resource_fields:
            value = payload.get(field)
            if f"{field}:{value}" not in principal.permitted_resources:
                raise PermissionError(f"principal is not authorized for {field}")
        self._catalog.verify_artifacts()
        with operation_authority.bind(principal):
            response = await self._catalog.client.run(operation, payload)
        return response.root
