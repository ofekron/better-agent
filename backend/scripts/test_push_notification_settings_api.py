from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-push-settings-api-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402

installation_profile.allows = lambda _capability: True


def main_runner() -> int:
    try:
        route = "/api/push-tokens/mobile-device/notification-preferences"
        with TestClient(
            main.app,
            client=("127.0.0.1", 54322),
        ) as authenticated:
            authenticate_client(authenticated)
            defaults = authenticated.get(route)
            patched = authenticated.patch(
                route,
                json={
                    "notification_preferences": {
                        "completed_turns": False,
                    },
                },
            )
            registered = authenticated.post(
                "/api/push-tokens",
                json={
                    "device_id": "mobile-device",
                    "token": "fcm-token",
                    "platform": "android",
                    "session_id": "session-1",
                },
            )
            preserved = authenticated.get(route)
            invalid = authenticated.patch(
                route,
                json={
                    "notification_preferences": {
                        "pending_questions": "yes",
                    },
                },
            )

        anonymous = TestClient(main.app, client=("127.0.0.1", 54323))
        unauthenticated = anonymous.get(route)

        expected_defaults = {
            "pending_approvals": True,
            "pending_questions": True,
            "completed_turns": True,
        }
        ok = (
            registered.status_code == 200
            and registered.json()["device"]["notification_preferences"]
            == {**expected_defaults, "completed_turns": False}
            and defaults.status_code == 200
            and defaults.json()["notification_preferences"] == expected_defaults
            and patched.status_code == 200
            and patched.json()["notification_preferences"]
            == {**expected_defaults, "completed_turns": False}
            and preserved.status_code == 200
            and preserved.json()["notification_preferences"]
            == {**expected_defaults, "completed_turns": False}
            and invalid.status_code == 400
            and unauthenticated.status_code == 401
        )
        if not ok:
            print(
                {
                    "registered": (registered.status_code, registered.text),
                    "defaults": (defaults.status_code, defaults.text),
                    "patched": (patched.status_code, patched.text),
                    "preserved": (preserved.status_code, preserved.text),
                    "invalid": (invalid.status_code, invalid.text),
                    "unauthenticated": (
                        unauthenticated.status_code,
                        unauthenticated.text,
                    ),
                }
            )
            return 1
        print(
            "PASS push notification settings API is durable, "
            "validated, and authenticated"
        )
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_runner())
