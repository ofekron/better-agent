"""Append-only audit log for the credential broker.

Every consent request, approval/denial/revocation, and execute attempt is
recorded — but NEVER the secret value, NEVER the operation result body, and
NEVER raw template strings (which can embed a templated secret position).
Only metadata: who/what/when/where/outcome. The audit log is itself a
potential leak surface, so it is scrubbed by construction (we only ever
write the fields below).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from paths import ba_home

_ALLOWED_FIELDS = (
    "event",
    "consent_id",
    "provider_id",
    "app_session_id",
    "computed_host",
    "computed_target",
    "risk",
    "outcome",
    "status_code",
)


def _path() -> Path:
    d = ba_home() / "credential_broker"
    d.mkdir(parents=True, exist_ok=True)
    return d / "audit.jsonl"


def record(
    event: str,
    *,
    consent_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    app_session_id: Optional[str] = None,
    computed_host: Optional[str] = None,
    computed_target: Optional[str] = None,
    risk: Optional[str] = None,
    outcome: Optional[str] = None,
    status_code: Optional[int] = None,
) -> None:
    entry = {"ts": datetime.now().isoformat(), "event": event}
    for k, v in (
        ("consent_id", consent_id),
        ("provider_id", provider_id),
        ("app_session_id", app_session_id),
        ("computed_host", computed_host),
        ("computed_target", computed_target),
        ("risk", risk),
        ("outcome", outcome),
        ("status_code", status_code),
    ):
        if v is not None:
            entry[k] = v
    # Defensive: never serialize a field outside the allowlist.
    entry = {k: v for k, v in entry.items() if k in ("ts",) or k in _ALLOWED_FIELDS}
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    p = _path()
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
