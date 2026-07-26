from __future__ import annotations

import hashlib

import paths
from keychain_names import home_suffix

AUTH_ACCOUNT = "oauth-session"
_SERVICE_PREFIX = "better-agent-marketplace"


def service_name() -> str:
    if not home_suffix():
        return _SERVICE_PREFIX
    home_digest = hashlib.sha256(str(paths.ba_home().resolve()).encode()).hexdigest()[
        :16
    ]
    return f"{_SERVICE_PREFIX}-{home_digest}"
