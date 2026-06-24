"""Provider identity pinning.

A credential request from a provider may only target hosts on that
provider's ``allowed_sinks`` pin. A compromised or malicious provider then
cannot redirect the secret to an attacker-controlled host: the request is
rejected BEFORE a pending consent is created, so the user never even sees
an off-pin request.

Fail-closed (CLAUDE.md): an empty / missing pin matches nothing. The pin is
user-controlled provider config (trusted because the user configured the
provider). A signed-manifest variant can later supply the same pin from a
provider-author key without changing this check.
"""

from __future__ import annotations


class PinViolation(Exception):
    """Raised when a descriptor's computed host is not on the provider pin."""


def host_allowed(computed_host: str, allowed_sinks: list[str]) -> bool:
    """True iff ``computed_host`` matches an entry in ``allowed_sinks``.

    Entries are host patterns:
      * ``api.github.com``  — exact host match
      * ``*.github.com``    — any subdomain of github.com (NOT github.com itself)
    """
    if not computed_host or not allowed_sinks:
        return False
    host = computed_host.lower()
    for raw in allowed_sinks:
        if not isinstance(raw, str) or not raw:
            continue
        pat = raw.lower().strip()
        if pat.startswith("*."):
            suffix = pat[1:]  # ".github.com"
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == pat:
            return True
    return False


def enforce(computed_host: str, allowed_sinks: list[str]) -> None:
    """Raise PinViolation unless the host is pinned. Fail-closed."""
    if not host_allowed(computed_host, allowed_sinks):
        raise PinViolation(
            f"host {computed_host!r} is not on the provider's allowed_sinks pin"
        )
