"""Regression: runner_openai must route Bash/Write/Edit approvals through the
backend HTTP client (tool_approval_client.request_tool_approval), NOT the
in-process tool_approval.registry.

The bug: runner_openai ran in a subprocess but created approval records in its
own in-process registry. The backend (separate process) never saw them, the
frontend never showed a prompt, and every approval silently timed out 5 minutes
later into "Error: tool use denied by user" — even though the user never denied
anything. Claude/Codex runners were unaffected (they already POST).
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_appr_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import importlib  # noqa: E402
import runner_openai  # noqa: E402
import tool_approval  # noqa: E402


def test_approval_routes_through_http_client_approved(monkeypatch):
    calls = []

    def fake_request_tool_approval(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(runner_openai, "request_tool_approval", fake_request_tool_approval)

    verdict = asyncio.run(runner_openai._request_approval(
        app_session_id="sid-1", run_id="run-1", tool_name="Bash",
        args={"command": "ls"}, backend_url="http://backend",
        internal_token="tok", cancel_path=Path(_TMP_HOME) / "nope-cancel",
    ))

    assert verdict == "approved"
    assert len(calls) == 1
    assert calls[0]["provider_kind"] == "openai"
    assert calls[0]["backend_url"] == "http://backend"
    assert calls[0]["internal_token"] == "tok"
    assert calls[0]["tool_name"] == "Bash"
    # Unified summary shape shared with the Claude/Codex runners: {"tool",
    # "input"} with every arg stringified + capped, so the one frontend card
    # renders them. (Was a divergent {"tool", "args"} the UI couldn't read.)
    assert calls[0]["summary"] == {"tool": "Bash", "input": {"command": "ls"}}


def test_approval_routes_through_http_client_denied(monkeypatch):
    monkeypatch.setattr(runner_openai, "request_tool_approval", lambda **kw: False)
    verdict = asyncio.run(runner_openai._request_approval(
        app_session_id="sid-2", run_id="run-2", tool_name="Write",
        args={"path": "x"}, backend_url="http://backend",
        internal_token="tok", cancel_path=Path(_TMP_HOME) / "nope-cancel-2",
    ))
    assert verdict == "denied"


def test_approval_does_not_touch_in_process_registry(monkeypatch):
    """The subprocess-local registry must NOT be used: the backend process
    can't see it, so any record there is invisible to the frontend and times
    out into a silent denial. Regression-lock that no record is created."""
    tool_approval.registry._pending.clear()

    monkeypatch.setattr(runner_openai, "request_tool_approval", lambda **kw: False)
    asyncio.run(runner_openai._request_approval(
        app_session_id="sid-3", run_id="run-3", tool_name="Edit",
        args={}, backend_url="http://backend", internal_token="tok",
        cancel_path=Path(_TMP_HOME) / "nope-cancel-3",
    ))

    assert tool_approval.registry.list_for_session("sid-3") == [], (
        "runner_openai must not create in-process approval records; route via "
        "tool_approval_client.request_tool_approval so the backend surfaces them"
    )


def test_request_approval_swallows_exception_as_denial(monkeypatch):
    """An exception out of the approval thread must resolve to 'denied', not
    propagate and abort the turn."""
    def raising(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_openai, "request_tool_approval", raising)
    verdict = asyncio.run(runner_openai._request_approval(
        app_session_id="sid-2b", run_id="run-2b", tool_name="Bash",
        args={}, backend_url="http://backend", internal_token="tok",
        cancel_path=Path(_TMP_HOME) / "nope-cancel-2b",
    ))
    assert verdict == "denied"


def test_cancel_aborts_in_flight_approval(monkeypatch):
    """A turn cancel must abort the in-flight approval instead of waiting for
    the backend's fail-closed timeout."""
    cancel_path = Path(_TMP_HOME) / "cancel-marker"
    cancel_path.write_text("")

    def slow_request(**kwargs):
        # Emulate the backend blocking until its timeout. The cancel race must
        # interrupt this well before 6 minutes.
        import time as _t
        _t.sleep(2)
        return True

    monkeypatch.setattr(runner_openai, "request_tool_approval", slow_request)
    verdict = asyncio.run(runner_openai._request_approval(
        app_session_id="sid-4", run_id="run-4", tool_name="Bash",
        args={"command": "x"}, backend_url="http://backend",
        internal_token="tok", cancel_path=cancel_path,
    ))

    assert verdict == "cancelled"


class _FakeEmitter:
    def __init__(self):
        self.results = []

    def emit_tool_result(self, call_id, content):
        self.results.append((call_id, content))


def test_dispatch_tool_gates_bash_and_emits_denial(monkeypatch):
    """End-to-end at the dispatch layer: a denied Bash emits the denial as the
    tool result; bypass=True skips the gate entirely."""
    monkeypatch.setattr(runner_openai, "request_tool_approval", lambda **kw: False)
    em = _FakeEmitter()
    call = {"id": "c1", "name": "Bash", "arguments": '{"command": "ls"}'}

    res = asyncio.run(runner_openai._dispatch_tool(
        call, cwd=Path(_TMP_HOME), app_session_id="sid-5",
        run_dir=Path(_TMP_HOME), bypass=False, interactive=True,
        backend_url="http://backend", internal_token="tok",
        emitter=em, loopback_handlers={},
    ))
    assert res == "Error: tool use denied by user"
    assert em.results == [("c1", "Error: tool use denied by user")]


def test_dispatch_tool_non_interactive_fails_closed_without_blaming_user(monkeypatch):
    """When the approval channel is missing, a risky tool must fail closed with
    an honest config message — never the user-blaming 'denied by user'."""
    approved_called = []
    monkeypatch.setattr(
        runner_openai, "request_tool_approval",
        lambda **kw: approved_called.append(kw) or True,
    )
    em = _FakeEmitter()
    call = {"id": "c2", "name": "Bash", "arguments": '{"command": "ls"}'}

    res = asyncio.run(runner_openai._dispatch_tool(
        call, cwd=Path(_TMP_HOME), app_session_id="sid-6",
        run_dir=Path(_TMP_HOME), bypass=False, interactive=False,
        backend_url="", internal_token="",
        emitter=em, loopback_handlers={},
    ))
    assert "approval channel unavailable" in res
    assert "denied by user" not in res
    assert approved_called == []  # gate never reached the HTTP client


def test_dispatch_tool_bypass_runs_handler(monkeypatch):
    """bypass=True skips the gate entirely; the handler runs."""
    monkeypatch.setattr(runner_openai, "request_tool_approval", lambda **kw: True)
    fired = []
    orig_bash = runner_openai.TOOL_HANDLERS.get("Bash")
    monkeypatch.setitem(runner_openai.TOOL_HANDLERS, "Bash",
                        lambda args, cwd: fired.append(args) or "ok")
    em = _FakeEmitter()
    call = {"id": "c1", "name": "Bash", "arguments": '{"command": "ls"}'}

    res = asyncio.run(runner_openai._dispatch_tool(
        call, cwd=Path(_TMP_HOME), app_session_id="sid-7",
        run_dir=Path(_TMP_HOME), bypass=True, interactive=False,
        backend_url="", internal_token="",
        emitter=em, loopback_handlers={},
    ))
    assert res == "ok"
    assert fired == [{"command": "ls"}]
    assert em.results == [("c1", "ok")]


if __name__ == "__main__":
    sys.exit(0)
