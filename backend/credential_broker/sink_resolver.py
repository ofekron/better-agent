"""Anti-deception: compute the LITERAL data-flow sink from the descriptor.

The approval UI must show the user where the secret bytes actually go —
derived from the operation spec, NEVER from the provider's free-text
``label``. A provider that labels its request "GitHub" but points the URL
at ``evil.com`` must be exposed: this module computes the real host and
flags the mismatch.

It also classifies:
  * ``egress``  — does the secret value leave this machine over the
                  network (vs. being consumed locally)? For http the secret
                  always egresses to ``computed_host``.
  * ``risk``    — "low" for idempotent/read-only ops, "high" for
                  state-changing / unknown ops. Fail-closed: anything not
                  provably low is high, which forces the per-op presence
                  gate downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from urllib.parse import urlparse

from credential_broker.descriptor import (
    IDEMPOTENT_HTTP_METHODS,
    SECRET_PLACEHOLDER,
)


@dataclass
class SinkInfo:
    sink_kind: str
    computed_host: str  # the real host the secret reaches
    computed_target: str  # human-readable: method + scheme://host/path
    egress: bool  # secret value leaves the machine over the network
    risk: str  # "low" | "high"
    risk_reasons: list[str] = field(default_factory=list)
    label_mismatch: bool = False  # provider label disagrees with computed host

    def to_public(self) -> dict:
        """Display-safe dict for the approval card / WS frame. No secret."""
        return {
            "sink_kind": self.sink_kind,
            "computed_host": self.computed_host,
            "computed_target": self.computed_target,
            "egress": self.egress,
            "risk": self.risk,
            "risk_reasons": list(self.risk_reasons),
            "label_mismatch": self.label_mismatch,
        }


def resolve(norm: dict) -> SinkInfo:
    """Compute the literal sink for a validated descriptor."""
    if norm["sink_kind"] == "http":
        return _resolve_http(norm)
    if norm["sink_kind"] == "local_keychain":
        return _resolve_local_keychain(norm)
    if norm["sink_kind"] == "exec":
        return _resolve_exec(norm)
    # Unknown kind should have been rejected in descriptor.validate; treat as
    # maximally suspicious rather than guessing.
    return SinkInfo(
        sink_kind=norm.get("sink_kind", "unknown"),
        computed_host="",
        computed_target="",
        egress=True,
        risk="high",
        risk_reasons=["unknown sink kind"],
    )


def _resolve_http(norm: dict) -> SinkInfo:
    sink = norm["sink"]
    # Parse the template with the placeholder neutralized so a secret inside
    # the URL can't itself shift the parsed host.
    url = sink["url_template"].replace(SECRET_PLACEHOLDER, "x")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    method = sink["method"]
    target = f"{method} {parsed.scheme}://{host}{parsed.path or ''}"

    reasons: list[str] = []
    risk = "low"
    if method not in IDEMPOTENT_HTTP_METHODS:
        risk = "high"
        reasons.append(f"non-idempotent method {method}")
    if not host:
        risk = "high"
        reasons.append("could not determine host")

    # The secret egresses to `host` whenever it appears in any part that is
    # transmitted (url/header/body/query) — which descriptor.validate already
    # guaranteed it does.
    egress = True

    return SinkInfo(
        sink_kind="http",
        computed_host=host,
        computed_target=target,
        egress=egress,
        risk=risk,
        risk_reasons=reasons,
        label_mismatch=_label_mismatch(norm.get("label", ""), host),
    )


def _resolve_local_keychain(norm: dict) -> SinkInfo:
    sink = norm["sink"]
    service = sink["service"]
    account = sink["account"]
    return SinkInfo(
        sink_kind="local_keychain",
        computed_host=f"local-keychain:{service}",
        computed_target=f"local keychain: {service}/{account}",
        egress=False,
        risk="low",
        risk_reasons=[],
        label_mismatch=False,
    )


def _resolve_exec(norm: dict) -> SinkInfo:
    sink = norm["sink"]
    argv = sink["argv"]
    binary = argv[0]
    return SinkInfo(
        sink_kind="exec",
        computed_host=f"exec:{binary}",
        computed_target=shlex.join(argv),
        egress=True,
        risk="high",
        risk_reasons=["local command can exfiltrate secret"],
        label_mismatch=False,
    )


def _label_mismatch(label: str, computed_host: str) -> bool:
    """True when the provider's free-text label names a host that disagrees
    with the computed host. Conservative: only flags when the label looks
    like it references a domain (contains a dot) and that domain is not a
    suffix of the computed host."""
    if not computed_host:
        return False
    label_l = label.lower()
    # pull dotted tokens out of the label
    for tok in _dotted_tokens(label_l):
        if computed_host == tok or computed_host.endswith("." + tok):
            return False  # label's domain matches the real host
    # if the label mentioned any domain-looking token but none matched → mismatch
    return any(True for _ in _dotted_tokens(label_l))


def _dotted_tokens(text: str):
    cur = []
    for ch in text:
        if ch.isalnum() or ch in ".-":
            cur.append(ch)
        else:
            tok = "".join(cur).strip(".-")
            if "." in tok:
                yield tok
            cur = []
    tok = "".join(cur).strip(".-")
    if "." in tok:
        yield tok
