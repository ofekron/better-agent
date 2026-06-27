"""Runner-subprocess side of the tool-approval round-trip.

Called from inside a runner (Claude `can_use_tool` callback, Codex app-server
approval handler) to ask the backend for a human decision. Blocks (sync, in a
thread) until the backend returns a verdict — the backend only responds once
the frontend decides or the fail-closed timeout fires. Any transport error or
non-approved response is treated as a DENIAL (fail-closed: never auto-approve)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("tool_approval_client")

# Must exceed backend tool_approval.APPROVAL_TIMEOUT_S so the backend, not the
# HTTP client, is the fail-closed authority.
_HTTP_TIMEOUT_S = 6 * 60


def request_tool_approval(
    *,
    backend_url: str,
    internal_token: str,
    app_session_id: str,
    run_id: str,
    provider_kind: str,
    tool_name: str,
    summary: dict,
) -> bool:
    """Return True only if the user approved. False on denial, timeout, or any
    error (fail-closed)."""
    if not backend_url or not internal_token or not app_session_id:
        return False
    body = json.dumps(
        {
            "app_session_id": app_session_id,
            "run_id": run_id,
            "provider_kind": provider_kind,
            "tool_name": tool_name,
            "summary": summary,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        backend_url.rstrip("/") + "/api/internal/tool-approvals/request",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return bool(payload.get("approved"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        logger.warning("tool-approval request failed (denying): %s", exc)
        return False
    except Exception as exc:
        logger.exception("tool-approval request unexpected error (denying): %s", exc)
        return False
