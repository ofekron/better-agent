"""Surface tests for the credential broker's backend wiring.

Covers the genuinely-new surface logic that the REST/MCP layer adds on top
of the (separately tested) broker core:

  * provider `allowed_sinks` pin persists via config_store (add/update/get)
    and is fail-closed for unknown providers — this is the authoritative
    state the internal `/api/internal/credential/request` endpoint reads.
  * the request endpoint's pin-resolution path: a descriptor is accepted
    only when its computed host is on the provider's persisted pin, exactly
    as `internal_credential_request` wires config_store → broker.
  * the WS event type used to invalidate the frontend is on the
    broadcast allowlist.

Run:
    cd backend && BETTER_CLAUDE_TEST_PRESENCE=allow .venv/bin/python scripts/test_credential_surface.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cred-surface-")
os.environ.setdefault("BETTER_CLAUDE_TEST_PRESENCE", "allow")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402
from credential_broker import broker  # noqa: E402


def _descriptor(url):
    return {
        "provider_id": "WILL_BE_SET",
        "label": "GitHub API",
        "sink_kind": "http",
        "sink": {
            "method": "GET",
            "url_template": url,
            "headers": {"Authorization": "Bearer {{secret}}"},
        },
    }


def test_allowed_sinks_persist_and_failclosed():
    p = config_store.add_provider(
        {
            "name": "GH",
            "kind": "claude",
            "mode": "subscription",
            # junk + dupes + case → cleaned
            "allowed_sinks": ["api.github.com", "API.GitHub.com", "", 123, "*.github.com"],
        }
    )
    pid = p["id"]
    assert "allowed_sinks" in p, "_strip must expose allowed_sinks"
    assert config_store.get_allowed_sinks(pid) == ["api.github.com", "*.github.com"]

    # update replaces
    config_store.update_provider(pid, {"allowed_sinks": ["api.example.com"]})
    assert config_store.get_allowed_sinks(pid) == ["api.example.com"]

    # fail-closed: unknown provider has no pin
    assert config_store.get_allowed_sinks("no-such-provider") == []
    print("ok  allowed_sinks persist + clean + fail-closed")


def test_request_pin_resolution_endpoint_path():
    """Mirror internal_credential_request: resolve pin from config_store,
    then broker.request_consent with it."""
    p = config_store.add_provider(
        {"name": "GH2", "kind": "claude", "mode": "subscription",
         "allowed_sinks": ["api.github.com"]}
    )
    pid = p["id"]

    # on-pin host → accepted, becomes a pending consent
    d_ok = _descriptor("https://api.github.com/user?t={{secret}}")
    d_ok["provider_id"] = pid
    allowed = config_store.get_allowed_sinks(pid)
    view = broker.request_consent(
        app_session_id="sid-1", descriptor_raw=d_ok, allowed_sinks=allowed
    )
    assert view["status"] == "pending"
    assert view["sink"]["computed_host"] == "api.github.com"

    # off-pin host → rejected before any pending consent exists
    d_bad = _descriptor("https://evil.com/x?t={{secret}}")
    d_bad["provider_id"] = pid
    try:
        broker.request_consent(
            app_session_id="sid-1", descriptor_raw=d_bad,
            allowed_sinks=config_store.get_allowed_sinks(pid),
        )
        raise AssertionError("off-pin descriptor must be rejected")
    except broker.BrokerError:
        pass
    print("ok  request pin-resolution (on-pin accepted, off-pin rejected)")


def test_ws_event_on_allowlist():
    from orchestrator import Coordinator

    assert "credential_consent_changed" in Coordinator.GLOBAL_EVENT_ALLOWLIST
    print("ok  WS event on broadcast allowlist")


def _run_all():
    tests = [
        test_allowed_sinks_persist_and_failclosed,
        test_request_pin_resolution_endpoint_path,
        test_ws_event_on_allowlist,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
            import traceback

            traceback.print_exc()
    return failed


if __name__ == "__main__":
    try:
        rc = _run_all()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if rc:
        print(f"\n{rc} test(s) failed")
        sys.exit(1)
    print("\nall credential-broker surface tests passed")
