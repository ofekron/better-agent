"""Unit test for Client.notify_toast's namespaced extension-event request.

Run with:
    cd sdk && python3 tests/test_client_notify_toast.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.dirname(_HERE)
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

import better_agent_sdk  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _client_with_captured_post():
    client = better_agent_sdk.Client(
        backend_url="http://localhost:1", internal_token="t", app_session_id="default-sid",
    )
    calls: list[tuple[str, dict]] = []
    client._post = lambda path, payload, **_: (calls.append((path, payload)), {"success": True})[1]
    return client, calls


def _notify_toast_posts_extension_toast_event() -> bool:
    client, calls = _client_with_captured_post()
    client.notify_toast("compacted 42%", session_id="sid-1", level="success")
    ok = (
        len(calls) == 1
        and calls[0][0] == "/api/internal/sessions/sid-1/broadcast"
        and calls[0][1]["event_name"] == "extension_toast"
        and calls[0][1]["data"] == {"message": "compacted 42%", "level": "success"}
    )
    print(f"{OK if ok else FAIL} notify_toast posts a single extension_toast broadcast (got {calls})")
    return ok


def _notify_toast_defaults_level_and_session() -> bool:
    client, calls = _client_with_captured_post()
    client.notify_toast("hello")
    ok = (
        calls[0][0] == "/api/internal/sessions/default-sid/broadcast"
        and calls[0][1]["data"]["level"] == "info"
    )
    print(f"{OK if ok else FAIL} notify_toast defaults session_id to app_session_id, level to info (got {calls})")
    return ok


def _notify_toast_merges_extra_data() -> bool:
    client, calls = _client_with_captured_post()
    client.notify_toast("compacted", session_id="sid-1", data={"percent": 42})
    ok = calls[0][1]["data"] == {"message": "compacted", "level": "info", "percent": 42}
    print(f"{OK if ok else FAIL} notify_toast merges extra data alongside message/level (got {calls})")
    return ok


_CHECKS = (
    _notify_toast_posts_extension_toast_event,
    _notify_toast_defaults_level_and_session,
    _notify_toast_merges_extra_data,
)


def test_notify_toast_contract() -> None:
    assert all(check() for check in _CHECKS)


def main_run() -> int:
    results = [check() for check in _CHECKS]
    n_pass = sum(1 for r in results if r)
    print(f"\n{n_pass}/{len(_CHECKS)} notify_toast tests passed")
    return 0 if n_pass == len(_CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main_run())
