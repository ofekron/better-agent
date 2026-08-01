from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

SCHEME = "betteragent"
PAIR_HOST = "marketplace"
PAIR_PATH = "/pair"
PROTOCOL_VERSION = 1
MAX_URL_LENGTH = 512
PAIR_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class DeepLinkError(ValueError):
    pass


@dataclass(frozen=True)
class MarketplacePairLink:
    intent: str
    version: int = PROTOCOL_VERSION

    def as_event(self) -> dict[str, str | int]:
        return {
            "type": "marketplace_pair",
            "intent": self.intent,
            "version": self.version,
        }


def _valid_token(value: str) -> bool:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):  # pragma: no cover — regex above restricts value to the urlsafe alphabet at length 43; with the single pad char added the input is a valid 44-char urlsafe string, so b64decode cannot raise. Defensive only.
        return False
    return (
        len(decoded) == PAIR_TOKEN_BYTES
        and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value
    )


def parse_deep_link(value: str) -> MarketplacePairLink:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        raise DeepLinkError("invalid deep-link length")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeepLinkError("invalid deep-link port") from exc
    if (
        parsed.scheme.casefold() != SCHEME
        or parsed.hostname != PAIR_HOST
        or parsed.path != PAIR_PATH
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port is not None
    ):
        raise DeepLinkError("unsupported deep link")
    prefix = f"v={PROTOCOL_VERSION}&intent="
    if not parsed.query.startswith(prefix):
        raise DeepLinkError("unexpected deep-link fields")
    intent = parsed.query[len(prefix) :]
    if not _valid_token(intent):
        raise DeepLinkError("invalid pair intent")
    canonical = f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?{prefix}{intent}"
    if value != canonical:
        raise DeepLinkError("non-canonical deep link")
    return MarketplacePairLink(intent=intent)


def deep_link_from_argv(argv: list[str]) -> MarketplacePairLink | None:
    links = [
        arg
        for arg in argv
        if isinstance(arg, str) and arg.casefold().startswith(f"{SCHEME}:")
    ]
    if not links:
        return None
    if len(links) != 1:
        raise DeepLinkError("multiple deep links")
    return parse_deep_link(links[0])


def redact_argv(argv: list[str]) -> list[str]:
    return [
        f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent=<redacted>"
        if isinstance(arg, str) and arg.casefold().startswith(f"{SCHEME}:")
        else arg
        for arg in argv
    ]
