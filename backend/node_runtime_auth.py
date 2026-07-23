from __future__ import annotations

import hmac
import secrets

_TOKEN = secrets.token_urlsafe(48)


def token() -> str:
    return _TOKEN


def verify(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, _TOKEN)
