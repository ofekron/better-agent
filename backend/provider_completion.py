from __future__ import annotations

from datetime import datetime
from typing import Any


RECOVERABLE_PARTIAL_OUTCOME = "recoverable_partial"
TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR = "transport_truncated_after_progress"


def recoverable_partial_payload(
    *,
    session_id: str,
    cause: str | None = None,
    token_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "outcome": RECOVERABLE_PARTIAL_OUTCOME,
        "recoverable": True,
        "session_id": session_id,
        "error": TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR,
        "cause": cause or None,
        "token_usage": token_usage or None,
        "finished_at": datetime.now().isoformat(),
    }
