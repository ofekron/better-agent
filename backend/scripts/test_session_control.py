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
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/better-agent-test.sock"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import continuation  # noqa: E402
import config_store  # noqa: E402
import extension_store  # noqa: E402
import installation_profile  # noqa: E402
import session_manager  # noqa: E402

installation_profile.integrations_enabled = lambda: True
installation_profile.allows = lambda _capability: True

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def test_continuation_requested_flag_roundtrip() -> None:
    sid = session_manager.manager.create(name="sc", cwd=str(TMP_HOME))["id"]
    try:
        assert session_manager.manager.pop_continuation_requested(sid) is None
        session_manager.manager.set_continuation_requested(sid, "keep going", when="next_turn")
        popped = session_manager.manager.pop_continuation_requested(sid)
        check(popped == {"prompt": "keep going", "reason": "agent_requested", "when": "next_turn"},
              "pop returns the queued next_turn request")
        session_manager.manager.set_continuation_requested(sid, "now-prompt", when="now")
        popped = session_manager.manager.pop_continuation_requested(sid)
        check(popped.get("when") == "now", "flag carries when=now")
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
    # Install the public package snapshot, then confirm runtime MCP injection.
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
    nv = extension_store.runtime_mcp_server_configs(
        inputs,
        user_facing=True,
        bare=False,
    )
    check("better-agent-session-control" in nv, "injected for native session")
    if "better-agent-session-control" in nv:
        env = nv["better-agent-session-control"]["env"]
        check(
            env.get("BETTER_AGENT_RUNTIME_BROKER")
            == "unix:/tmp/better-agent-test.sock",
            "session-control receives the scoped runtime broker",
        )
        check(
            "BETTER_AGENT_INTERNAL_TOKEN" not in env,
            "session-control receives no bearer token",
        )
    excluded = extension_store.runtime_mcp_server_configs(
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

        provider = config_store.add_provider({
            "name": "Session Control Provider",
            "kind": "claude",
            "mode": "subscription",
            "default_model": "model-one",
            "custom_models": ["model-one", "model-two"],
        })
        sid = session_manager.manager.create(
            name="sc-ep",
            cwd=str(TMP_HOME),
            provider_id=provider["id"],
            model="model-one",
            orchestration_mode="native",
        )["id"]
        session_manager.manager.append_assistant_msg(sid, {
            "id": "assistant-switch-model",
            "role": "assistant",
            "content": "",
            "events": [],
            "isStreaming": True,
        })
        try:
            # continue-fresh next_turn (default): sets the flag, no live turn.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "prompt": "next step"},
            )
            check(resp.status_code == 200, f"continue-fresh next_turn ok ({resp.status_code})")
            check(resp.json().get("when") == "next_turn", "next_turn reflected in response")
            req = (session_manager.manager.get(sid) or {}).get("continuation_requested")
            check(req == {"prompt": "next step", "reason": "agent_requested", "when": "next_turn"},
                  "continue-fresh next_turn set the flag")

            # continue-fresh with when="now" but NO live turn → falls back to next_turn.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "prompt": "p", "when": "now"},
            )
            check(resp.status_code == 200, f"continue-fresh now-no-turn ok ({resp.status_code})")
            check(resp.json().get("when") == "next_turn", "now with no live turn falls back to next_turn")

            # continue-fresh with when="now" AND a live cancel_event → aborts + flag=now.
            import asyncio
            tm = main.coordinator.turn_manager
            tm.cancel_events[sid] = asyncio.Event()
            try:
                resp = client.post(
                    "/api/internal/session-control/continue-fresh",
                    headers={"X-Internal-Token": internal_token},
                    json={"app_session_id": sid, "prompt": "abort and restart", "when": "now"},
                )
                check(resp.status_code == 200, f"continue-fresh now-live ok ({resp.status_code})")
                check(resp.json().get("when") == "now", "now reflected when a live turn is aborted")
                check(tm.cancel_events[sid].is_set(), "now sets the cancel event (aborts in-flight run)")
                req = (session_manager.manager.get(sid) or {}).get("continuation_requested")
                check(req.get("when") == "now", "continue-fresh now set the flag with when=now")
            finally:
                tm.cancel_events.pop(sid, None)

            # continue-fresh rejects an invalid when.
            resp = client.post(
                "/api/internal/session-control/continue-fresh",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "prompt": "x", "when": "bogus"},
            )
            check(resp.status_code == 400, "continue-fresh rejects invalid when")

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

            # Agent-driven model switching is currently disabled: reject with
            # 409 instead of applying it, and leave the session's model
            # untouched (regression for the model_provider-mismatch ghost bug).
            resp = client.post(
                "/api/internal/session-control/selectors",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "model": "model-two"},
            )
            check(resp.status_code == 409, f"selectors rejects agent model switch ({resp.status_code})")
            session = session_manager.manager.get(sid) or {}
            check(session.get("model") == "model-one", "model unchanged after rejected switch")
            assistant = next(
                (m for m in session.get("messages", []) if m.get("id") == "assistant-switch-model"),
                {},
            )
            switch_events = [
                e for e in assistant.get("events", [])
                if e.get("type") == "model_switched"
            ]
            check(len(switch_events) == 0, "no model_switched event appended for a rejected switch")

            # provider_id-only switch is rejected the same way.
            resp = client.post(
                "/api/internal/session-control/selectors",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "provider_id": provider["id"]},
            )
            check(resp.status_code == 409, f"selectors rejects agent provider switch ({resp.status_code})")

            # reasoning_effort-only switch still works (no provider/model
            # identity risk).
            resp = client.post(
                "/api/internal/session-control/selectors",
                headers={"X-Internal-Token": internal_token},
                json={"app_session_id": sid, "reasoning_effort": "high"},
            )
            check(resp.status_code == 200, f"selectors still allows reasoning_effort-only ({resp.status_code})")

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
