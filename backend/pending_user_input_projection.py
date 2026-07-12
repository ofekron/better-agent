from __future__ import annotations

from typing import Any

import user_input_store


def snapshot(app_session_id: str) -> dict[str, Any]:
    revision, requests = user_input_store.pending_snapshot_for_session(app_session_id)
    return {
        "app_session_id": app_session_id,
        "revision": revision,
        "requests": requests,
    }
