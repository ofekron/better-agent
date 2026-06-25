"""Regression: runner_better_agent must not silently truncate agentic turns.

Pre-fix, `_run` capped the tool loop at `_MAX_TOOL_LOOPS = 40` and, when the
cap was exhausted without the model emitting a terminal response, left
`error=None` and wrote `complete.json` with `success=True` — so a tool-heavy
multi-agent model (Sakana Fugu) was cut off mid-task and reported as a
successful completion. Real fugu runs were observed hitting exactly 40 rounds
with a trailing tool_result and `success=True`.

Post-fix: the cap is overridable via `inputs["max_tool_loops"]`, the default
is high enough for agentic models to finish naturally, and cap-exhaustion is
reported as `success=False` with an explicit error (for/else on the loop).

Two cases, both driven against a stub Chat Completions server:
  (A) 60 tool rounds then a text reply → reaches the natural stop (pre-fix
      truncated at round 40, never reaching the reply).
  (B) model never stops + max_tool_loops=3 → success=False with a cap error
      (pre-fix reported success=True — the regression).
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_cap_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import runner_better_agent  # noqa: E402


def _sse_chunks(chunks: list) -> bytes:
    out = b""
    for c in chunks:
        out += b"data: " + json.dumps(c).encode() + b"\n\n"
    out += b"data: [DONE]\n\n"
    return out


def _text_reply(content: str) -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                  "total_tokens": 6}},
    ])


def _tool_call_reply(call_id: str) -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": "Bash",
                         "arguments": json.dumps({"command": "echo hi"})},
        }]}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                  "total_tokens": 7}},
    ])


class _Stub:
    """Stub Chat Completions server. `responder(idx) -> SSE bytes` decides
    the response for each call. Thread-safe request counter."""

    def __init__(self, responder):
        self.requests = 0
        self.responder = responder
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                with server.lock:
                    idx = server.requests
                    server.requests += 1
                    data = server.responder(idx)
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
    rd = parent / f"run_{inputs['app_session_id']}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
    return rd


def _base_inputs(tmp: Path, *, app_sid: str, extra: dict | None = None) -> dict:
    inputs = {
        "prompt": "do-some-work", "images": [], "files": [], "cwd": str(tmp),
        "model": "stub-model", "reasoning_effort": None,
        "permission": {"default": "bypass"},  # CLI vocab {axis: mode}
        "session_id": None, "mode": "native", "app_session_id": app_sid,
        "backend_url": "", "internal_token": "",
    }
    if extra:
        inputs.update(extra)
    return inputs


def test_long_agentic_turn_reaches_natural_stop(monkeypatch):
    """60 tool rounds then a text reply: with the raised+overridable cap the
    runner reaches the model's natural stop. Pre-fix (cap=40, no override)
    truncated at round 40 and never reached the text reply."""
    stub = _Stub(lambda i: _tool_call_reply(f"call_{i}") if i < 60
                 else _text_reply("all-done"))
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    # Bash must not actually shell out 60 times; stub it to an instant reply.
    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash",
                        lambda args, cwd: "tool-output")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_cap_long_"))
        rd = _make_run_dir(tmp, _base_inputs(tmp, app_sid="sid-cap-long"))
        rc = asyncio.run(runner_better_agent._run(rd, _base_inputs(tmp, app_sid="sid-cap-long")))
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    assert rc == 0, f"runner exited {rc}: {complete}"
    # reached the text reply past round 60 — pre-fix stopped at 40 calls.
    assert stub.requests >= 61, f"truncated at {stub.requests} rounds (cap hit)"
    assert complete["success"] is True, complete
    assert "max tool loops" not in (complete.get("error") or ""), complete
    # the natural-stop text reply actually landed in the event stream
    events = [(rd / "session_events.jsonl").read_text()]
    assert "all-done" in events[0], "natural-stop text reply missing"


def test_cap_exhaustion_is_reported_as_failure(monkeypatch):
    """Model never stops (always tool_calls) + max_tool_loops=3: the turn must
    be reported as not-completed (success=False, cap error). Pre-fix this case
    wrote success=True — the regression."""
    stub = _Stub(lambda i: _tool_call_reply(f"call_{i}"))
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash",
                        lambda args, cwd: "tool-output")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_cap_short_"))
        inputs = _base_inputs(tmp, app_sid="sid-cap-short",
                              extra={"max_tool_loops": 3})
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    assert rc == 1, f"runner exited {rc} (expected non-zero): {complete}"
    assert stub.requests == 3, f"expected 3 rounds, got {stub.requests}"
    assert complete["success"] is False, complete
    assert "max tool loops" in (complete.get("error") or ""), complete


if __name__ == "__main__":
    sys.exit(0)
