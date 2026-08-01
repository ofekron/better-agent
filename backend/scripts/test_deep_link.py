"""Promoted pytest coverage for desktop/deep_link.py — marketplace pair deep-link parsing.

deep_link.py is pure-stdlib (base64/re/dataclasses/urllib) parser logic that was
previously only exercised by desktop standalone runners (desktop/test_app_main.py
covers redact_argv; desktop/test_marketplace_deep_link.py covers the full flow),
which are NOT pytest-collected, so it measured 0% in the unit tier. This module
ports that coverage into the pytest unit tier and closes the remaining branches.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

from deep_link import (  # noqa: E402
    DeepLinkError,
    MAX_URL_LENGTH,
    PAIR_HOST,
    PAIR_PATH,
    PROTOCOL_VERSION,
    SCHEME,
    MarketplacePairLink,
    _valid_token,
    deep_link_from_argv,
    parse_deep_link,
    redact_argv,
)

# urlsafe base64 alphabet, value order.
_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _valid_intent() -> str:
    """A canonical 32-byte pair intent token (43 urlsafe chars, round-trips)."""
    return base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def _canonical_url(intent: str) -> str:
    return f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={intent}"


def _non_canonical_encoding(intent: str) -> str:
    """Same decoded bytes as `intent` but with the trailing 2 base64 bits set
    nonzero. The regex still matches, b64decode yields identical 32 bytes, but
    re-encoding canonicalizes those bits to 0 — so `_valid_token` rejects it."""
    last_idx = _B64_ALPHABET.index(intent[-1])  # canonical → divisible by 4
    return intent[:-1] + _B64_ALPHABET[last_idx + 1]


# --------------------------------------------------------------------------- #
# _valid_token
# --------------------------------------------------------------------------- #

def test_valid_token_accepts_canonical_round_trip():
    token = _valid_intent()
    assert _valid_token(token) is True


def test_valid_token_rejects_regex_violations():
    assert _valid_token("") is False                 # too short
    assert _valid_token("A" * 5) is False            # wrong length
    assert _valid_token("!" * 43) is False           # illegal chars
    # correct length but contains a non-alphabet char
    assert _valid_token(_valid_intent()[:-1] + "!") is False


def test_valid_token_rejects_non_canonical_encoding():
    assert _valid_token(_non_canonical_encoding(_valid_intent())) is False


# --------------------------------------------------------------------------- #
# parse_deep_link — happy path
# --------------------------------------------------------------------------- #

def test_parse_deep_link_round_trips_canonical_url():
    intent = _valid_intent()
    link = parse_deep_link(_canonical_url(intent))
    assert isinstance(link, MarketplacePairLink)
    assert link.intent == intent
    assert link.version == PROTOCOL_VERSION


def test_marketplace_pair_link_as_event_shape():
    link = MarketplacePairLink(intent="abc")
    assert link.as_event() == {
        "type": "marketplace_pair",
        "intent": "abc",
        "version": PROTOCOL_VERSION,
    }


# --------------------------------------------------------------------------- #
# parse_deep_link — length guards
# --------------------------------------------------------------------------- #

def test_parse_deep_link_rejects_non_string():
    with pytest.raises(DeepLinkError, match="length"):
        parse_deep_link(123)  # type: ignore[arg-type]


def test_parse_deep_link_rejects_overlong():
    with pytest.raises(DeepLinkError, match="length"):
        parse_deep_link("x" * (MAX_URL_LENGTH + 1))


# --------------------------------------------------------------------------- #
# parse_deep_link — port + unsupported-link arms (each OR condition)
# --------------------------------------------------------------------------- #

def test_parse_deep_link_rejects_invalid_port():
    url = f"{SCHEME}://{PAIR_HOST}:99999{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}"
    with pytest.raises(DeepLinkError, match="port"):
        parse_deep_link(url)


@pytest.mark.parametrize(
    "url,desc",
    [
        (f"http://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "scheme"),
        (f"{SCHEME}://other-host{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "hostname"),
        (f"{SCHEME}://{PAIR_HOST}/other?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "path"),
        (f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}#frag", "fragment"),
        (f"{SCHEME}://user@{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "username"),
        # ":pw@host" → username '' (falsy) but password set → exercises the password arm.
        (f"{SCHEME}://:pw@{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "password"),
        (f"{SCHEME}://{PAIR_HOST}:8000{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={_valid_intent()}", "port"),
    ],
)
def test_parse_deep_link_rejects_unsupported(url, desc):
    with pytest.raises(DeepLinkError, match="unsupported"):
        parse_deep_link(url)


# --------------------------------------------------------------------------- #
# parse_deep_link — query / intent / canonical arms
# --------------------------------------------------------------------------- #

def test_parse_deep_link_rejects_unexpected_query_fields():
    url = f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?other=stuff"
    with pytest.raises(DeepLinkError, match="fields"):
        parse_deep_link(url)


def test_parse_deep_link_rejects_invalid_intent():
    url = _canonical_url("not-a-valid-token")
    with pytest.raises(DeepLinkError, match="intent"):
        parse_deep_link(url)


def test_parse_deep_link_rejects_non_canonical_form():
    # Uppercase host survives urlsplit's lowercasing (passes the host check) but
    # the raw URL no longer equals the reconstructed canonical form.
    intent = _valid_intent()
    url = f"{SCHEME}://Marketplace{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={intent}"
    with pytest.raises(DeepLinkError, match="non-canonical"):
        parse_deep_link(url)


# --------------------------------------------------------------------------- #
# deep_link_from_argv
# --------------------------------------------------------------------------- #

def test_deep_link_from_argv_returns_none_when_no_link():
    assert deep_link_from_argv([]) is None
    assert deep_link_from_argv(["Better Agent", "--serve"]) is None


def test_deep_link_from_argv_raises_on_multiple_links():
    intent = _valid_intent()
    url = _canonical_url(intent)
    with pytest.raises(DeepLinkError, match="multiple"):
        deep_link_from_argv([url, url])


def test_deep_link_from_argv_parses_single_link():
    intent = _valid_intent()
    link = deep_link_from_argv(["Better Agent", _canonical_url(intent)])
    assert isinstance(link, MarketplacePairLink)
    assert link.intent == intent


# --------------------------------------------------------------------------- #
# redact_argv
# --------------------------------------------------------------------------- #

def test_redact_argv_replaces_link_and_preserves_rest():
    intent = _valid_intent()
    argv = ["Better Agent", _canonical_url(intent), "--serve"]
    redacted = redact_argv(argv)
    assert redacted[0] == "Better Agent"
    assert redacted[2] == "--serve"
    assert intent not in " ".join(redacted)
    assert redacted[1] == (
        f"{SCHEME}://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent=<redacted>"
    )


def test_redact_argv_passes_through_non_link_args():
    assert redact_argv(["a", "b"]) == ["a", "b"]


def test_redact_argv_is_case_insensitive_on_scheme():
    intent = _valid_intent()
    argv = [f"{SCHEME.upper()}://{PAIR_HOST}{PAIR_PATH}?v={PROTOCOL_VERSION}&intent={intent}"]
    assert intent not in " ".join(redact_argv(argv))
