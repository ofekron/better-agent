#!/usr/bin/env python3
"""session-control extension: agent-driven model switching + continuation.

Covers:
- session_manager continuation_requested flag round-trip
- continuation "agent_requested" reason renders
- extension manifest validity + runtime injection (native launcher)
- internal endpoints: continue-fresh sets the flag; selectors gating
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-session-control-"))
import _test_home  # noqa: E402
_test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import continuation  # noqa: E402
import extension_store  # noqa: E402
import session_manager  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def test_continuation_requested_flag_roundtrip() -> None:
    sid = session_manager.manager.create(name="sc", cwd=str(TMP_HOME))["id"]
    try:
        assert session_manager.manager.pop_continuation_requested(sid) is None
        session_manager.manager.set_continuation_requested(sid, "keep going", "agent_requested")
        popped = session_manager.manager.pop_continuation_requested(sid)
        check(popped == {"prompt": "keep going", "reason": "agent_requested"},
              "pop returns the queued continuation request")
        check(session_manager.manager.pop_continuation_requested(sid) is None,
              "pop clears the flag (second pop is None)")
    finally:
        session_manager.manager.delete(sid)


def test_agent_requested_continuation_prompt() -> None:
    prompt = continuation.build_continuation_prompt(
        prompt="do X", app_session_id="s1", continuation_chain=["old"],
        reason="agent_requested",
    )
    check("agent requested a fresh context window" in prompt, "agent_requested reason rendered")
    check("do X" in prompt and "s1" in prompt and "old" in prompt, "prompt + chain rendered")


def test_session_control_extension_validates_and_injects() -> None:
    check(extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID
          in extension_store._PUBLIC_EXTENSION_PATHS, "registered in public paths")
    manifest_path = ROOT.parent / "extensions" / "session-control" / "better-agent-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validated = extension_store.validate_manifest(manifest)
    check(validated["surfaces"] == ["runtime_mcp"], "runtime_mcp surface only")
    # Install the public package snapshot, then confirm native-launcher injection.
    data = extension_store._load()  # type: ignore[attr-defined]
    extension_store._ensure_public_extensions(data)  # type: ignore[attr-defined]
    extension_store._save(data)  # type: ignore[attr-defined]
    rec = extension_store._load()["extensions"].get(  # type: ignore[attr-defined]
        extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID
    )
    check(rec is not None and rec.get("enabled"), "session-control auto-installs enabled")
    inputs = {
        "mode": "native", "app_session_id": "s1", "working_mode": "native",
        "open_file_panel_enabled": True, "backend_url": "http://localhost:8000",
        "internal_token": "tok",
    }
    nv = extension_store.native_mcp_launcher_server_configs(inputs, user_facing=True, bare=False)
    check("better-agent-session-control" in nv, "injected for native session")
    excluded = extension_store.native_mcp_launcher_server_configs(
        {**inputs, "working_mode": "search_worker"}, user_facing=True, bare=False,
    )
    check("better-agent-session-control" not in excluded, "excluded for search_worker")


def test_endpoints() -> None:
    from fastapi.testclient import TestClient
    import auth
    import main

    with TestClient(main.app) as client:
        client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
        internal_token = getattr(main.coordinator, "internal_token", "")

        # Extension must be runtime-ready for the endpoints to be reachable.
        data = extension_store._load()  # type: ignore[attr-defined]
        extension_store._ensure_public_extensions(data)  # type: ignore[attr-defined]
        extension_store._save(data)  # type: ignore[attr-defined]

        sid = session_manager.manager.create(name="sc-ep", cwd=str(TMP_HOME))["id"]
        try:
            # continue-fresh: sets the continuation flag on the caller's session.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "prompt": "next step"},
            )
            check(resp.status_code == 200, f"continue-fresh ok ({resp.status_code})")
            req = (session_manager.manager.get(sid) or {}).get("continuation_requested")
            check(req == {"prompt": "next step", "reason": "agent_requested"},
                  "continue-fresh set the continuation flag")

            # continue-fresh rejects a missing prompt.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "prompt": ""},
            )
            check(resp.status_code == 400, "continue-fresh rejects empty prompt")

            # selectors rejects a missing app_session_id.
            resp = client.post(
                "/api/internal/session-control/selectors",
                headers={"X-Internal-Token": internal_token},
                json={"model": "anything"},
            )
            check(resp.status_code == 400, "selectors rejects missing app_session_id")

            # Bad internal token is forbidden.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": "not-a-real-token"},
                json={"app_session_id": sid, "prompt": "x"},
            )
            check(resp.status_code == 403, "bad internal token rejected")
        finally:
            session_manager.manager.delete(sid)


def main_runner() -> int:
    test_continuation_requested_flag_roundtrip()
    test_agent_requested_continuation_prompt()
    test_session_control_extension_validates_and_injects()
    test_endpoints()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nok")
    return 0


if __name__ == "__main__":
    rc = main_runner()
    shutil.rmtree(TMP_HOME, ignore_errors=True)
    raise SystemExit(rc)
