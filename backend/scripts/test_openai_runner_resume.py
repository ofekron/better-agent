"""Empirical: does runner_openai resume conversation history on turn 2+?

Drives the real `_run` loop twice against a stubbed Chat Completions server,
passing the discovered session_id from turn 1 into turn 2. Asserts the stub
saw turn 1's user prompt + assistant reply in turn 2's request messages.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_resume_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import runner_openai  # noqa: E402


def _sse_lines(*, content: str) -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8}},
    ])


def _sse_chunks(chunks: list) -> bytes:
    out = b""
    for c in chunks:
        out += b"data: " + json.dumps(c).encode() + b"\n\n"
    out += b"data: [DONE]\n\n"
    return out


def _reasoning_only_response() -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {"reasoning_content": "thinking..."},
                      "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                  "total_tokens": 6}},
    ])


def _tool_call_then_text_response(*, call_idx: int) -> bytes:
    """call 0: a Bash tool_call; call 1: plain text reply."""
    if call_idx == 0:
        return _sse_chunks([
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "Bash",
                             "arguments": json.dumps({"command": "echo hi"})},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                      "total_tokens": 7}},
        ])
    return _sse_lines(content="after-tool-reply")


class _Stub:
    """Stub Chat Completions server. `responses` maps call index -> raw SSE
    bytes. Unmapped calls get a default text reply."""

    def __init__(self, responses: dict | None = None):
        self.requests = []  # list of parsed message-arrays
        self.responses = responses or {}
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                with server.lock:
                    idx = len(server.requests)
                    server.requests.append(payload.get("messages", []))
                    data = server.responses.get(idx)
                if data is None:
                    data = _sse_lines(content=f"default-reply-{idx}")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _make_run_dir(parent: Path, inputs: dict) -> Path:
    rd = parent / f"run_{inputs['app_session_id']}_{inputs.get('session_id') or 'fresh'}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
    return rd


def _run_turn(stub_port: str, tmp: Path, app_sid: str, resume_sid, prompt: str):
    inputs = {
        "prompt": prompt,
        "images": [], "files": [],
        "cwd": str(tmp),
        "model": "stub-model",
        "reasoning_effort": None,
        "permission": {"default": "bypass"},  # CLI vocab {axis: mode}
        "session_id": resume_sid,
        "mode": "native",
        "app_session_id": app_sid,
        "backend_url": "", "internal_token": "",
    }
    rd = _make_run_dir(tmp, inputs)
    rc = asyncio.run(runner_openai._run(rd, inputs))
    assert rc == 0, f"runner exited {rc}"
    state = json.loads((rd / "state.json").read_text())
    complete = json.loads((rd / "complete.json").read_text())
    assert complete["success"], f"turn failed: {complete.get('error')}"
    return state["session_id"]


def test_openai_runner_resumes_history_across_turns(monkeypatch):
    # point the runner's httpx calls at our stub via env
    stub = _Stub({0: _sse_lines(content="A1-reply"),
                  1: _sse_lines(content="A2-reply")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_cwd_"))

        sid1 = _run_turn(None, tmp, "sid-app-1", None, "Q1-prompt")
        assert sid1, "turn 1 produced no session_id"

        sid2 = _run_turn(None, tmp, "sid-app-1", sid1, "Q2-prompt")
        assert sid2 == sid1, f"session_id changed across turns: {sid1} -> {sid2}"

        with stub.lock:
            reqs = [list(m) for m in stub.requests]
    finally:
        stub.stop()

    assert len(reqs) == 2, f"expected 2 stub calls, got {len(reqs)}"

    turn1_prompts = [m.get("content") for m in reqs[0] if m.get("role") == "user"]
    turn2_prompts = [m.get("content") for m in reqs[1] if m.get("role") == "user"]
    turn2_assistants = [
        m.get("content") for m in reqs[1]
        if m.get("role") == "assistant" and m.get("content")
    ]

    # turn 1: only Q1
    assert turn1_prompts == ["Q1-prompt"], turn1_prompts
    # turn 2: must carry turn 1's Q1 + reply AND the new Q2
    assert "Q1-prompt" in turn2_prompts, "turn 2 lost turn 1's user prompt"
    assert "Q2-prompt" in turn2_prompts, "turn 2 lost its own prompt"
    assert any("A1-reply" in (c or "") for c in turn2_assistants), (
        "turn 2 lost turn 1's assistant reply"
    )


def _no_invalid_assistant(messages) -> bool:
    """True if no assistant message has content=None and no tool_calls
    (the shape that 400s on strict OpenAI-compatible endpoints)."""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        if m.get("content") is None and not m.get("tool_calls"):
            return False
    return True


def test_reasoning_only_round_is_not_persisted_as_null(monkeypatch):
    """A round that streams only reasoning_content (no text, no tool_calls)
    must not leave an invalid {content: null, no tool_calls} assistant message
    in the saved history — that would 400 the next turn's resume."""
    stub = _Stub({0: _reasoning_only_response(),
                  1: _sse_lines(content="real-reply")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_reason_"))
        sid1 = _run_turn(None, tmp, "sid-app-r", None, "Q-reason")

        # inspect the persisted openai_sessions history directly
        hist_path = runner_openai._session_path(sid1)
        history = json.loads(hist_path.read_text())["messages"]
        assert _no_invalid_assistant(history), (
            "persisted history contains an invalid null-content assistant message"
        )

        # and the same invariant holds in what turn 2 actually POSTs
        _run_turn(None, tmp, "sid-app-r", sid1, "Q-reason-2")
        with stub.lock:
            posted = stub.requests[-1]
    finally:
        stub.stop()
    assert _no_invalid_assistant(posted), (
        "turn 2 POSTed an invalid null-content assistant message"
    )


def test_tool_call_then_text_preserves_both(monkeypatch):
    """A turn that emits a tool_call (round 1) then text (round 2 in the same
    loop) must persist: assistant{content=None, tool_calls}, tool{result},
    assistant{content=text}. All three shapes are schema-valid."""
    stub = _Stub({0: _tool_call_then_text_response(call_idx=0),
                  1: _tool_call_then_text_response(call_idx=1)})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    fired = []
    monkeypatch.setitem(runner_openai.TOOL_HANDLERS, "Bash",
                        lambda args, cwd: fired.append(args) or "tool-output")

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_tool_"))
        sid1 = _run_turn(None, tmp, "sid-app-t", None, "use-a-tool")
        history = json.loads(
            runner_openai._session_path(sid1).read_text())["messages"]
    finally:
        stub.stop()

    assert fired == [{"command": "echo hi"}], "Bash handler never ran"
    assert _no_invalid_assistant(history), history

    roles = [(m.get("role"),
              bool(m.get("tool_calls")),
              "content" in m and m.get("content") is not None)
             for m in history if m.get("role") in ("assistant", "tool")]
    # expect: assistant(with tool_calls, maybe no text) -> tool -> assistant(text)
    assert any(r[0] == "assistant" and r[1] for r in roles), "no tool_call assistant msg"
    assert any(r[0] == "tool" for r in roles), "no tool result msg"
    assert any(r[0] == "assistant" and r[2] and not r[1] for r in roles), (
        "no follow-up text assistant msg after the tool call"
    )


if __name__ == "__main__":
    sys.exit(0)
