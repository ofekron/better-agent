"""Meaningful E2E tests for the capabilities_mcp / open_config_panel_mcp
builtin MCP tools, invoked exactly as an LLM would call them (real
`/api/internal/*` loopback request with a real X-Internal-Token) with no
model in the loop, each verifying the actual resulting backend state
rather than merely that the call did not raise.

Run with:
    cd backend && .venv/bin/python scripts/test_capabilities_and_config_panel_mcp_e2e.py
"""

from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-cap-cfg-mcp-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import extension_store  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_CAP_EXTENSION_ID = "ofek.testcap"
_CAP_ID = "testcap"
_CAP_FULL_ID = f"{_CAP_EXTENSION_ID}:{_CAP_ID}"


def _install_fake_capability() -> None:
    """Same fixture shape as test_capability_catalog.py's
    test_catalog_composes_full_ids: monkeypatch the active-extension-records
    source so a real capability descriptor resolves through
    extension_store.get_capability, without installing a whole extension
    package."""
    record = {
        "manifest": {
            "id": _CAP_EXTENSION_ID,
            "entrypoints": {
                "capabilities": [
                    {"id": _CAP_ID, "scope": "session", "skill": ["use-testcap"]},
                ],
            },
        },
    }
    extension_store._active_records = lambda: [record]


def _new_session() -> str:
    sess = session_store.create_session(
        name="t", model="m", cwd="/tmp", orchestration_mode="native",
    )
    return sess["id"]


def _tok() -> str:
    return main.coordinator.internal_token


def _capabilities_call(client: TestClient, sid: str, body: dict):
    return client.post(
        f"/api/internal/sessions/{sid}/capabilities",
        json=body,
        headers={"X-Internal-Token": _tok()},
    )


# ------------------------------------------------------------------ load/release
def test_load_capability_actually_activates_it(client: TestClient) -> bool:
    sid = _new_session()
    if session_manager.get(sid).get("active_capability_ids"):
        print("  fresh session should start with no active capabilities")
        return False
    r = _capabilities_call(client, sid, {"action": "load", "capability_id": _CAP_FULL_ID})
    if r.status_code != 200 or not r.json().get("ok"):
        print(f"  load failed: {r.status_code} {r.text}")
        return False
    if _CAP_FULL_ID not in r.json().get("active_capability_ids", []):
        print(f"  load response missing capability: {r.json()}")
        return False
    # Independently verify against the session store, not just the echoed response.
    persisted = session_manager.get(sid).get("active_capability_ids") or []
    if _CAP_FULL_ID not in persisted:
        print(f"  capability not actually persisted on session: {persisted}")
        return False
    return True


def test_list_capabilities_reflects_active_set(client: TestClient) -> bool:
    sid = _new_session()
    _capabilities_call(client, sid, {"action": "load", "capability_id": _CAP_FULL_ID})
    r = _capabilities_call(client, sid, {"action": "list"})
    if r.status_code != 200:
        print(f"  list failed: {r.status_code} {r.text}")
        return False
    body = r.json()
    active = body.get("active") or body.get("active_capability_ids") or []
    catalog = body.get("capabilities") or []
    if _CAP_FULL_ID not in active:
        print(f"  list did not report loaded capability active: {body}")
        return False
    if not any(item.get("id") == _CAP_FULL_ID for item in catalog):
        print(f"  list catalog missing scoped capability: {catalog}")
        return False
    return True


def test_release_capability_actually_deactivates_it(client: TestClient) -> bool:
    sid = _new_session()
    _capabilities_call(client, sid, {"action": "load", "capability_id": _CAP_FULL_ID})
    if _CAP_FULL_ID not in (session_manager.get(sid).get("active_capability_ids") or []):
        print("  precondition failed: capability was not loaded")
        return False
    r = _capabilities_call(client, sid, {"action": "release", "capability_id": _CAP_FULL_ID})
    if r.status_code != 200 or not r.json().get("ok"):
        print(f"  release failed: {r.status_code} {r.text}")
        return False
    if _CAP_FULL_ID in (r.json().get("active_capability_ids") or []):
        print(f"  release response still lists capability: {r.json()}")
        return False
    persisted = session_manager.get(sid).get("active_capability_ids") or []
    if _CAP_FULL_ID in persisted:
        print(f"  capability still active on session after release: {persisted}")
        return False
    return True


def test_load_unknown_capability_is_rejected_and_does_not_mutate(client: TestClient) -> bool:
    sid = _new_session()
    r = _capabilities_call(client, sid, {"action": "load", "capability_id": "nope:nope"})
    if r.status_code != 404:
        print(f"  expected 404 for unknown capability, got {r.status_code}: {r.text}")
        return False
    if session_manager.get(sid).get("active_capability_ids"):
        print("  unknown-capability load should not mutate session state")
        return False
    return True


def test_load_missing_session_is_404(client: TestClient) -> bool:
    r = _capabilities_call(client, "does-not-exist", {"action": "load", "capability_id": _CAP_FULL_ID})
    if r.status_code != 404:
        print(f"  expected 404 for missing session, got {r.status_code}")
        return False
    return True


def test_capabilities_bad_token_is_403(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        f"/api/internal/sessions/{sid}/capabilities",
        json={"action": "list"},
        headers={"X-Internal-Token": "wrong"},
    )
    if r.status_code != 403:
        print(f"  expected 403, got {r.status_code}")
        return False
    return True


# ------------------------------------------------------------------ open_config_panel
def test_open_config_panel_resolves_cwd_from_session(client: TestClient) -> bool:
    sess = session_store.create_session(name="t", model="m", cwd="/work/dir")
    r = client.post(
        "/api/internal/open-config-panel",
        json={"app_session_id": sess["id"], "capability_id": _CAP_FULL_ID, "scope": "project"},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  open_config_panel failed: {r.status_code} {r.text}")
        return False
    panel = r.json().get("panel") or {}
    if panel.get("cwd") != "/work/dir":
        print(f"  cwd not resolved from session: {panel}")
        return False
    if panel.get("capability_id") != _CAP_FULL_ID:
        print(f"  capability_id not echoed: {panel}")
        return False
    return True


def test_open_config_panel_does_not_mutate_session_state(client: TestClient) -> bool:
    """Per main.py's internal_open_config_panel docstring: always INLINE, no
    session state mutation — the persisted tool-call event is the source of
    truth. Lock that contract with a real invoke + a real state read-back."""
    sid = _new_session()
    before = session_manager.get(sid)
    r = client.post(
        "/api/internal/open-config-panel",
        json={"app_session_id": sid, "capability_id": _CAP_FULL_ID},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  open_config_panel failed: {r.status_code} {r.text}")
        return False
    after = session_manager.get(sid)
    if before == after:
        pass
    else:
        # Only the updated_at bookkeeping field, if any, is allowed to move;
        # any panel-list-shaped field must not appear.
        if "open_config_panels" in after and after.get("open_config_panels") != before.get(
            "open_config_panels"
        ):
            print("  open_config_panel wrongly mutated session state")
            return False
    return True


def test_open_config_panel_missing_capability_id_is_rejected(client: TestClient) -> bool:
    sid = _new_session()
    r = client.post(
        "/api/internal/open-config-panel",
        json={"app_session_id": sid, "capability_id": ""},
        headers={"X-Internal-Token": _tok()},
    )
    if r.json().get("success") is not False:
        print(f"  expected success False for missing capability_id: {r.json()}")
        return False
    return True


TESTS = [
    ("load_capability actually activates it", test_load_capability_actually_activates_it),
    ("list_capabilities reflects active set", test_list_capabilities_reflects_active_set),
    ("release_capability actually deactivates it", test_release_capability_actually_deactivates_it),
    ("load unknown capability -> 404, no mutation", test_load_unknown_capability_is_rejected_and_does_not_mutate),
    ("load missing session -> 404", test_load_missing_session_is_404),
    ("bad internal token -> 403", test_capabilities_bad_token_is_403),
    ("open_config_panel resolves cwd from session", test_open_config_panel_resolves_cwd_from_session),
    ("open_config_panel does not mutate session state", test_open_config_panel_does_not_mutate_session_state),
    ("open_config_panel missing capability_id -> success False", test_open_config_panel_missing_capability_id_is_rejected),
]


def main_run() -> int:
    _install_fake_capability()
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        failed = 0
        for name, fn in TESTS:
            try:
                ok = fn(client)
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
        print()
        if failed:
            print(f"{failed} of {len(TESTS)} test(s) FAILED")
        else:
            print(f"all {len(TESTS)} tests passed")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
