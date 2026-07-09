from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import auth


TICKET_MAX_AGE_SECONDS = 20 * 60
_NAMESPACE = "better-agent-mobile-bundle-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth.get_session_secret(), salt=_NAMESPACE)


def create_ticket(version: str, checksum: str) -> str:
    return _serializer().dumps({"version": version, "checksum": checksum})


def verify_ticket(ticket: str, version: str, checksum: str) -> bool:
    try:
        payload = _serializer().loads(ticket, max_age=TICKET_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return payload == {"version": version, "checksum": checksum}
