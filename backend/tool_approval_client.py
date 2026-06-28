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

# Per-field display cap. The summary rides a WS broadcast to every tab, so a
# huge value (a whole-file Write, a giant patch) must not be sent verbatim —
# that would bloat the payload and risk leaking large/secret blobs into the UI.
# The card shows enough to make the permission decision, not the full content.
_SUMMARY_VALUE_CAP = 500


def describe_tool_call(tool_name: object, tool_input: object) -> dict:
    """Build the approval-card summary for a tool call.

    Returns the ONE shape every runner must emit and the frontend relies on:
    ``{"tool": <str>, "input": {<arg>: <stringified, capped str>}}``. EVERY
    argument is preserved (the card renders them all so the user sees exactly
    what they're approving), with non-string values JSON-encoded and each value
    truncated to keep the summary small and secret-safe. A non-dict input
    degrades to empty args rather than raising."""
    raw = tool_input if isinstance(tool_input, dict) else {}
    described: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, default=str)
            except Exception:
                text = str(value)
        described[str(key)] = text[:_SUMMARY_VALUE_CAP]
    return {"tool": str(tool_name), "input": described}


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
