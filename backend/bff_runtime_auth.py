from __future__ import annotations

from bff_runtime_contract import BFF_SERVICE_SCOPE, BFF_SERVICE_TOKEN_KIND
import runtime_tokens


def is_authorized(raw_token: object) -> bool:
    record = runtime_tokens.TokenResolver().resolve(raw_token)
    return (
        isinstance(record, dict)
        and record.get("kind") == BFF_SERVICE_TOKEN_KIND
        and BFF_SERVICE_SCOPE in (record.get("scopes") or [])
    )
