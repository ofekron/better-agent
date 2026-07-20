from __future__ import annotations

import re
import logging


_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|ticket)=)[^&#\s]+"
)
_PATH_SECRET_RE = re.compile(
    r"(?i)(/api/(?:file/preview|task-output/preview)/)[^/?\s]+"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)(\b(?:token|access_token|refresh_token|ticket)\s*[:=]\s*)[^\s,;]+"
)


def redact_secrets(value: str) -> str:
    redacted = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", value)
    redacted = _PATH_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    return _NAMED_SECRET_RE.sub(r"\1[REDACTED]", redacted)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.msg))
        if record.args:
            record.args = tuple(
                redact_secrets(value) if isinstance(value, str) else value
                for value in record.args
            )
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(item, SecretRedactionFilter) for item in logger.filters):
        return
    logger.addFilter(SecretRedactionFilter())
