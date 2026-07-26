from __future__ import annotations

from keychain_names import home_suffix

AUTH_ACCOUNT = "oauth-session"
_SERVICE_PREFIX = "better-agent-marketplace"


def service_name() -> str:
    suffix = home_suffix()
    return f"{_SERVICE_PREFIX}-{suffix}" if suffix else _SERVICE_PREFIX
