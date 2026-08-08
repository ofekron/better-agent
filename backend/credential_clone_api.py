from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

import config_store

router = APIRouter(tags=["provider-credentials"])
_internal_token_matches: Callable[[str], bool] | None = None


class CloneProviderCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_provider_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[^\x00\r\n]+$",
    )
    target_provider_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[^\x00\r\n]+$",
    )

    @model_validator(mode="after")
    def distinct_provider_ids(self) -> "CloneProviderCredentialRequest":
        if self.source_provider_id == self.target_provider_id:
            raise ValueError("provider ids must be distinct")
        return self


def configure(internal_token_matches: Callable[[str], bool]) -> None:
    global _internal_token_matches
    _internal_token_matches = internal_token_matches


def _require_internal(token: str) -> None:
    if _internal_token_matches is None or not _internal_token_matches(token):
        raise HTTPException(status_code=403, detail="invalid internal token")


@router.post("/api/internal/provider-credentials/clone")
async def clone_provider_credential(
    body: CloneProviderCredentialRequest,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
) -> dict[str, str]:
    _require_internal(x_internal_token)
    try:
        status = await asyncio.to_thread(
            config_store.clone_provider_credential,
            body.source_provider_id,
            body.target_provider_id,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="provider credential authority is unavailable",
        ) from exc
    if status == "missing":
        raise HTTPException(status_code=404, detail="source credential is missing")
    if status != "available":
        raise HTTPException(
            status_code=503,
            detail="provider credential authority is blocked",
        )
    return {"status": "available"}
