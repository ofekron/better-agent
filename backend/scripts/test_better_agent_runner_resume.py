"""Empirical: does runner_better_agent resume conversation history on turn 2+?

Drives the real `_run` loop twice against a stubbed Chat Completions server,
passing the discovered session_id from turn 1 into turn 2. Asserts the stub
saw turn 1's user prompt + assistant reply in turn 2's request messages.
"""

import asyncio
import base64
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

import runner_better_agent  # noqa: E402


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


def _incremental_deltas_with_repeats_response() -> bytes:
    # Z.AI streams INCREMENTAL deltas. This sequence mixes a partial-prefix
    # progression ("a","ab","abc") and exact repeats ("\n","\n" and "x","x").
    # Every delta MUST be concatenated verbatim -> "aabc\n\nxx". A prefix-diff
    # "normalizer" would drop the repeats and collapse the prefix progression
    # to "abc\nx".
    return _sse_chunks([
        {"choices": [{"delta": {
            "reasoning_content": "a", "content": "a",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "ab", "content": "ab",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "abc", "content": "abc",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "\n", "content": "\n",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "\n", "content": "\n",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "x", "content": "x",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": "x", "content": "x",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8}},
    ])


def _prefix_looking_delta_response() -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {"content": "abc"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "abcde"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8}},
    ])


def _normal_delta_text_and_reasoning_response() -> bytes:
    return _sse_chunks([
        {"choices": [{"delta": {
            "reasoning_content": "Let",
            "content": "Hel",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": " me",
            "content": "lo",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {
            "reasoning_content": " analyze",
            "content": " world",
        }, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8}},
    ])


def _incremental_tool_args_with_repeat_response(*, call_idx: int) -> bytes:
    # Incremental tool-argument deltas whose concatenation is valid JSON, with
    # a repeated token ("a","a") -> {"command": "aa"}. A prefix-diff normalizer
    # would drop the second "a" and yield {"command": "a"}.
    if call_idx == 0:
        return _sse_chunks([
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "Bash", "arguments": "{\"command\": \""},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": "a"},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": "a"},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": "\"}"},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                      "total_tokens": 7}},
        ])
    return _sse_lines(content="after-tool-reply")


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
        self.payloads = []  # full parsed request bodies
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
                    server.payloads.append(payload)
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


def _run_turn(stub_port: str, tmp: Path, app_sid: str, resume_sid, prompt: str, **overrides):
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
    inputs.update(overrides)
    rd = _make_run_dir(tmp, inputs)
    rc = asyncio.run(runner_better_agent._run(rd, inputs))
    assert rc == 0, f"runner exited {rc}"
    state = json.loads((rd / "state.json").read_text())
    complete = json.loads((rd / "complete.json").read_text())
    assert complete["success"], f"turn failed: {complete.get('error')}"
    return state["session_id"]


def test_better_agent_runner_resumes_history_across_turns(monkeypatch):
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


def test_run_populates_token_usage_duration_ms(monkeypatch):
    # duration_ms used to be hardcoded to None; analytics consumes it, so a
    # completed turn must report a non-negative integer measured across the run.
    stub = _Stub({0: _sse_lines(content="hi")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_dur_cwd_"))
        inputs = {
            "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"default": "bypass"}, "session_id": None,
            "mode": "native", "app_session_id": "dur-app-1",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        assert rc == 0, f"runner exited {rc}"
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    tu = complete["token_usage"]
    assert tu["input_tokens"] == 5 and tu["output_tokens"] == 3, tu
    dur = tu["duration_ms"]
    assert isinstance(dur, int) and dur >= 0, f"duration_ms not populated: {dur!r}"


def _no_invalid_assistant(messages) -> bool:
    """True if no assistant message has content=None and no tool_calls
    (the shape that 400s on strict OpenAI-compatible endpoints)."""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        if m.get("content") is None and not m.get("tool_calls"):
            return False
    return True


def _event_texts(run_dir: Path, block_type: str, field: str) -> list[str]:
    values = []
    for line in (run_dir / "session_events.jsonl").read_text().splitlines():
        event = json.loads(line)
        content = event.get("message", {}).get("content") or []
        if content and content[0].get("type") == block_type:
            values.append(content[0].get(field) or "")
    return values


def _user_contents(messages):
    return [m.get("content") for m in messages if m.get("role") == "user"]


def test_fork_copies_history_to_isolated_child(monkeypatch):
    stub = _Stub({0: _sse_lines(content="parent-reply"),
                  1: _sse_lines(content="child-reply"),
                  2: _sse_lines(content="parent-again")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_fork_"))
        parent_sid = _run_turn(None, tmp, "sid-app-f", None, "parent-Q")
        child_sid = _run_turn(
            None, tmp, "sid-app-f", parent_sid, "child-Q", fork=True,
        )
        assert child_sid and child_sid != parent_sid
        _run_turn(None, tmp, "sid-app-f", parent_sid, "parent-Q2")
        parent_history = json.loads(
            runner_better_agent._session_path(parent_sid).read_text())["messages"]
        child_history = json.loads(
            runner_better_agent._session_path(child_sid).read_text())["messages"]
    finally:
        stub.stop()

    assert "parent-Q" in _user_contents(child_history)
    assert "child-Q" in _user_contents(child_history)
    assert "child-Q" not in _user_contents(parent_history)
    assert "parent-Q2" in _user_contents(parent_history)


def test_images_files_and_reasoning_are_sent(monkeypatch):
    file_payload = base64.b64encode(b"hello file").decode("ascii")
    img_payload = base64.b64encode(b"fakepng").decode("ascii")
    stub = _Stub({0: _sse_lines(content="mm-reply")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_mm_"))
        _run_turn(
            None, tmp, "sid-app-mm", None, "describe",
            files=[{"name": "a.txt", "data": file_payload, "size": 10}],
            images=[{"media_type": "image/png", "data": img_payload}],
            reasoning_effort="medium",
        )
        with stub.lock:
            payload = stub.payloads[0]
    finally:
        stub.stop()

    assert payload["reasoning_effort"] == "medium"
    user = next(m for m in payload["messages"] if m.get("role") == "user")
    content = user["content"]
    assert isinstance(content, list), content
    text = next(part["text"] for part in content if part.get("type") == "text")
    assert "<file name=\"a.txt\">" in text and "hello file" in text
    img = next(part for part in content if part.get("type") == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_steer_payload_drained_before_next_round(monkeypatch):
    stub = _Stub({0: _tool_call_then_text_response(call_idx=0),
                  1: _sse_lines(content="steered-reply")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    def steering_bash(args, cwd):
        # Simulate provider_openai.steer_run appending while the first tool is
        # running. The next model round should include this as a user message.
        run_dir = Path(os.environ["OPENAI_TEST_RUN_DIR"])
        (run_dir / "steer.jsonl").write_text(
            json.dumps({"prompt": "please adjust", "images": []}) + "\n",
            encoding="utf-8",
        )
        return "tool-output"

    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", steering_bash)
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_steer_"))
        inputs = {
            "prompt": "use tool", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": None,
            "mode": "native", "app_session_id": "sid-app-steer",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        monkeypatch.setenv("OPENAI_TEST_RUN_DIR", str(rd))
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        assert rc == 0
        with stub.lock:
            second_messages = stub.requests[1]
    finally:
        stub.stop()

    texts = [m.get("content") for m in second_messages if m.get("role") == "user"]
    assert any("please adjust" in str(t) for t in texts), texts


def test_zai_incremental_deltas_concatenated_verbatim(monkeypatch):
    # Z.AI's coding endpoint streams INCREMENTAL deltas. Repeated tokens and
    # partial-prefix progressions must be concatenated verbatim — never
    # prefix-diffed, which would drop them. Gates on the real coding base_url.
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    tmp = Path(tempfile.mkdtemp(prefix="openai_resume_zai_incremental_"))
    inputs = {
        "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
        "model": "stub-model", "reasoning_effort": None,
        "permission": {"default": "bypass"}, "session_id": None,
        "mode": "native", "app_session_id": "zai-incremental-app",
        "backend_url": "", "internal_token": "",
    }
    rd = _make_run_dir(tmp, inputs)

    async def fake_stream(*_args, **_kwargs):
        for raw in _incremental_deltas_with_repeats_response().split(b"\n\n"):
            if not raw.startswith(b"data: "):
                continue
            payload = raw[len(b"data: "):]
            if payload == b"[DONE]":
                return
            yield json.loads(payload)

    monkeypatch.setattr(runner_better_agent, "_stream_chat", fake_stream)
    rc = asyncio.run(runner_better_agent._run(rd, inputs))
    assert rc == 0, f"runner exited {rc}"
    complete = json.loads((rd / "complete.json").read_text())
    history = json.loads(
        runner_better_agent._session_path(complete["session_id"]).read_text()
    )["messages"]

    expected = "aababc\n\nxx"
    assert _event_texts(rd, "thinking", "thinking")[-1] == expected
    assert _event_texts(rd, "text", "text")[-1] == expected
    assistant = [m for m in history if m.get("role") == "assistant"][-1]
    assert assistant["content"] == expected
    assert assistant["reasoning_content"] == expected


def test_zai_normal_deltas_still_concatenate(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    tmp = Path(tempfile.mkdtemp(prefix="openai_resume_zai_delta_"))
    inputs = {
        "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
        "model": "stub-model", "reasoning_effort": None,
        "permission": {"default": "bypass"}, "session_id": None,
        "mode": "native", "app_session_id": "zai-delta-app",
        "backend_url": "", "internal_token": "",
    }
    rd = _make_run_dir(tmp, inputs)

    async def fake_stream(*_args, **_kwargs):
        for raw in _normal_delta_text_and_reasoning_response().split(b"\n\n"):
            if not raw.startswith(b"data: "):
                continue
            payload = raw[len(b"data: "):]
            if payload == b"[DONE]":
                return
            yield json.loads(payload)

    monkeypatch.setattr(runner_better_agent, "_stream_chat", fake_stream)
    rc = asyncio.run(runner_better_agent._run(rd, inputs))
    assert rc == 0, f"runner exited {rc}"
    complete = json.loads((rd / "complete.json").read_text())
    history = json.loads(
        runner_better_agent._session_path(complete["session_id"]).read_text()
    )["messages"]

    assert _event_texts(rd, "thinking", "thinking")[-1] == "Let me analyze"
    assert _event_texts(rd, "text", "text")[-1] == "Hello world"
    assistant = [m for m in history if m.get("role") == "assistant"][-1]
    assert assistant["content"] == "Hello world"
    assert assistant["reasoning_content"] == "Let me analyze"


def test_zai_incremental_tool_args_concatenated_verbatim(monkeypatch):
    # Incremental tool-argument deltas with a repeated token must concatenate
    # verbatim into {"command": "aa"} — a prefix-diff normalizer would drop the
    # second "a" and dispatch {"command": "a"}.
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    calls = []

    async def fake_stream(*_args, **_kwargs):
        response = _incremental_tool_args_with_repeat_response(call_idx=len(calls))
        for raw in response.split(b"\n\n"):
            if not raw.startswith(b"data: "):
                continue
            payload = raw[len(b"data: "):]
            if payload == b"[DONE]":
                return
            yield json.loads(payload)

    def fake_bash(args, _cwd):
        calls.append(args)
        return "tool-output"

    monkeypatch.setattr(runner_better_agent, "_stream_chat", fake_stream)
    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", fake_bash)
    tmp = Path(tempfile.mkdtemp(prefix="openai_resume_zai_tool_args_"))
    inputs = {
        "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
        "model": "stub-model", "reasoning_effort": None,
        "permission": {"default": "bypass"}, "session_id": None,
        "mode": "native", "app_session_id": "zai-tool-args-app",
        "backend_url": "", "internal_token": "",
    }
    rd = _make_run_dir(tmp, inputs)
    rc = asyncio.run(runner_better_agent._run(rd, inputs))

    assert rc == 0, f"runner exited {rc}"
    assert calls == [{"command": "aa"}]


def test_non_zai_prefix_looking_deltas_are_not_rewritten(monkeypatch):
    stub = _Stub({0: _prefix_looking_delta_response()})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_prefix_delta_"))
        inputs = {
            "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"default": "bypass"}, "session_id": None,
            "mode": "native", "app_session_id": "prefix-delta-app",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        assert rc == 0, f"runner exited {rc}"
        complete = json.loads((rd / "complete.json").read_text())
        history = json.loads(
            runner_better_agent._session_path(complete["session_id"]).read_text()
        )["messages"]
    finally:
        stub.stop()

    assert _event_texts(rd, "text", "text")[-1] == "abcabcde"
    assistant = [m for m in history if m.get("role") == "assistant"][-1]
    assert assistant["content"] == "abcabcde"


def test_zai_non_coding_prefix_looking_deltas_are_not_rewritten(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.z.ai/api/paas/v4")
    tmp = Path(tempfile.mkdtemp(prefix="openai_resume_zai_non_coding_delta_"))
    inputs = {
        "prompt": "Q", "images": [], "files": [], "cwd": str(tmp),
        "model": "stub-model", "reasoning_effort": None,
        "permission": {"default": "bypass"}, "session_id": None,
        "mode": "native", "app_session_id": "zai-non-coding-delta-app",
        "backend_url": "", "internal_token": "",
    }
    rd = _make_run_dir(tmp, inputs)

    async def fake_stream(*_args, **_kwargs):
        for raw in _prefix_looking_delta_response().split(b"\n\n"):
            if not raw.startswith(b"data: "):
                continue
            payload = raw[len(b"data: "):]
            if payload == b"[DONE]":
                return
            yield json.loads(payload)

    monkeypatch.setattr(runner_better_agent, "_stream_chat", fake_stream)
    rc = asyncio.run(runner_better_agent._run(rd, inputs))
    assert rc == 0, f"runner exited {rc}"
    complete = json.loads((rd / "complete.json").read_text())
    history = json.loads(
        runner_better_agent._session_path(complete["session_id"]).read_text()
    )["messages"]

    assert _event_texts(rd, "text", "text")[-1] == "abcabcde"
    assistant = [m for m in history if m.get("role") == "assistant"][-1]
    assert assistant["content"] == "abcabcde"


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

        # inspect the persisted better_agent_sessions history directly
        hist_path = runner_better_agent._session_path(sid1)
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


def test_cancel_after_partial_stream_is_failure(monkeypatch):
    """If a cancel sentinel appears while streaming, the runner must not
    persist the partial assistant text as a successful turn."""
    stub = _Stub({0: _sse_lines(content="partial-before-cancel")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    original_feed = runner_better_agent.EventEmitter.feed_text_delta

    def cancelling_feed(self, chunk):
        original_feed(self, chunk)
        Path(self._fp.name).parent.joinpath("cancel").write_text("", encoding="utf-8")

    monkeypatch.setattr(runner_better_agent.EventEmitter, "feed_text_delta", cancelling_feed)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_cancel_"))
        inputs = {
            "prompt": "cancel-me", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": None,
            "mode": "native", "app_session_id": "sid-app-c",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    assert rc == 1, complete
    assert complete["success"] is False
    assert complete["error"] == "cancelled"


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
    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash",
                        lambda args, cwd: fired.append(args) or "tool-output")

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_tool_"))
        sid1 = _run_turn(None, tmp, "sid-app-t", None, "use-a-tool")
        history = json.loads(
            runner_better_agent._session_path(sid1).read_text())["messages"]
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


def _no_dangling_tool_calls(messages) -> bool:
    for idx, m in enumerate(messages):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        need = {str(c.get("id")) for c in m["tool_calls"] if c.get("id")}
        have = {
            str(t.get("tool_call_id"))
            for t in messages[idx + 1:]
            if t.get("role") == "tool"
        }
        if not need.issubset(have):
            return False
    return True


def test_concurrent_history_writes_do_not_share_temp_path(monkeypatch):
    sid = "concurrenttmp0000000000000000000a"
    path = runner_better_agent._session_path(sid)
    deterministic_tmp = path.with_suffix(path.suffix + ".tmp")
    original_replace = runner_better_agent.os.replace
    barrier = threading.Barrier(2)
    calls: list[Path] = []

    def replace_with_old_tmp_collision(src, dst):
        src_path = Path(src)
        calls.append(src_path)
        if src_path == deterministic_tmp:
            barrier.wait(timeout=2)
        return original_replace(src, dst)

    monkeypatch.setattr(runner_better_agent.os, "replace", replace_with_old_tmp_collision)

    errors: list[BaseException] = []

    def save(content: str) -> None:
        try:
            runner_better_agent._save_history(
                sid,
                [{"role": "user", "content": content}],
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=save, args=("first",))
    second = threading.Thread(target=save, args=("second",))
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["messages"][0]["content"] in {"first", "second"}
    assert deterministic_tmp not in calls


def test_inflight_turn_persists_context_before_completion(monkeypatch):
    """REGRESSION: the reported bug — resume/continuation on the openai
    runner reported "no context" on turn 2.

    Root cause was that history was saved ONLY at the end of `_run`. A turn
    that never reached normal completion (still in-flight when the next turn
    started, cancelled, or killed mid-loop) left ZERO durable context, so the
    next turn resumed empty and answered "I don't have the previous context".

    This asserts that at the moment a tool runs — i.e. BEFORE the turn
    completes — the durable on-disk history already contains the user prompt,
    so a concurrent/next resume can see it. The model has emitted tool_calls
    by then, but those calls are not yet balanced by tool results, so the
    incremental snapshot must trim that dangling assistant block.
    """
    stub = _Stub({0: _tool_call_then_text_response(call_idx=0),
                  1: _sse_lines(content="final-after-tool")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    sid_preset = "inflightsid000000000000000000000a"
    snapshots: list[list[dict]] = []

    def snapshotting_tool(args, cwd):
        # At the moment the tool runs, the user prompt + assistant tool_call
        # must ALREADY be durable on disk (incremental persistence). Capture
        # what a concurrent resume would load right now.
        try:
            disk = json.loads(
                runner_better_agent._session_path(sid_preset).read_text()
            )["messages"]
        except Exception:
            disk = []
        snapshots.append(disk)
        return "tool-output"

    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", snapshotting_tool)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_inflight_"))
        # Pre-seed empty history so _load_history_for_run resumes a known sid,
        # keeping the on-disk path deterministic for the mid-run snapshot.
        runner_better_agent._save_history(sid_preset, [])
        inputs = {
            "prompt": "important-task", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-inflight",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        assert rc == 0
    finally:
        stub.stop()

    assert snapshots, "Bash tool never ran"
    mid = snapshots[0]
    user_contents = [m.get("content") for m in mid if m.get("role") == "user"]
    assert "important-task" in user_contents, (
        "mid-flight (before turn completion) the user prompt was NOT durable — "
        "a concurrent resume would see empty context (the reported bug)"
    )
    # The mid-flight snapshot must be resume-safe: no dangling assistant
    # tool_calls without matching tool results. Since the snapshot was taken
    # before the tool result was appended, the assistant tool_call block must
    # not be persisted yet; the final turn save below restores it once balanced.
    assert _no_dangling_tool_calls(mid), mid
    assert not any(m.get("tool_calls") for m in mid if m.get("role") == "assistant"), mid

    final_history = json.loads(
        runner_better_agent._session_path(sid_preset).read_text()
    )["messages"]
    assert any(
        m.get("tool_calls") for m in final_history if m.get("role") == "assistant"
    ), final_history
    assert any(m.get("role") == "tool" for m in final_history), final_history
    assert _no_dangling_tool_calls(final_history), final_history


def test_second_run_resumes_mid_flight_history_and_proceeds(monkeypatch):
    """MUST-FIX #1 (adversarial): prove the actual user-facing resume, not
    just the on-disk shape.

    Turn 1 DIES before normal completion (a cancel arrives mid tool-loop, the
    same shape as a SIGKILL/restart mid-turn). With end-of-`_run`-only saving
    this left empty durable history and turn 2 answered "no context". Here a
    REAL second `_run` resumes the same session id, and we assert the stub the
    model talks to in turn 2 actually receives turn 1's user prompt — i.e. the
    resumed process loaded mid-flight context and proceeded with it.
    """
    stub = _Stub({
        0: _tool_call_then_text_response(call_idx=0),  # turn 1: tool call
        1: _sse_lines(content="resumed-reply"),         # turn 2: plain reply
    })
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    sid_preset = "resumemidsid0000000000000000000a"

    def cancelling_tool(args, cwd):
        # The tool runs, its result is appended+persisted (balanced), then the
        # turn is cancelled before reaching normal completion — i.e. turn 1
        # never finishes cleanly, exactly the reported failure mode.
        run_dir = Path(os.environ["OPENAI_RESUME_RUN_DIR"])
        (run_dir / "cancel").write_text("", encoding="utf-8")
        return "tool-output"

    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", cancelling_tool)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_resume_mid_"))
        # ---- turn 1: dies mid-flight (cancelled) ----
        inputs1 = {
            "prompt": "important-task", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-resume-mid",
            "backend_url": "", "internal_token": "",
        }
        rd1 = tmp / "run_turn1"
        rd1.mkdir(parents=True, exist_ok=True)
        (rd1 / "input.json").write_text(json.dumps(inputs1), encoding="utf-8")
        monkeypatch.setenv("OPENAI_RESUME_RUN_DIR", str(rd1))
        rc1 = asyncio.run(runner_better_agent._run(rd1, inputs1))
        # turn 1 did not complete normally
        complete1 = json.loads((rd1 / "complete.json").read_text())
        assert rc1 == 1 and complete1["error"] == "cancelled", complete1

        # context from the dead turn must already be durable on disk
        mid_disk = json.loads(
            runner_better_agent._session_path(sid_preset).read_text())["messages"]
        assert "important-task" in _user_contents(mid_disk), (
            "turn 1 died without leaving its prompt durable (the reported bug)"
        )
        assert _no_dangling_tool_calls(mid_disk), mid_disk

        # ---- turn 2: a REAL second _run resumes the same session ----
        inputs2 = {
            "prompt": "follow-up", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-resume-mid",
            "backend_url": "", "internal_token": "",
        }
        rd2 = tmp / "run_turn2"
        rd2.mkdir(parents=True, exist_ok=True)
        (rd2 / "input.json").write_text(json.dumps(inputs2), encoding="utf-8")
        rc2 = asyncio.run(runner_better_agent._run(rd2, inputs2))
        assert rc2 == 0
        with stub.lock:
            turn2_req = stub.requests[-1]
    finally:
        stub.stop()

    turn2_users = _user_contents(turn2_req)
    assert "important-task" in turn2_users, (
        "resumed turn 2 did NOT carry turn 1's prompt — the model would report "
        "no context (the exact user-facing bug)"
    )
    assert "follow-up" in turn2_users, turn2_users
    # And the resumed request must itself be a valid resume (no dangling block).
    assert _no_dangling_tool_calls(turn2_req), turn2_req


def test_capability_context_excluded_from_durable_history(monkeypatch):
    """MUST-FIX #2 (adversarial): capability-context exclusion is durable.

    The per-turn capability system message is transient scaffolding for THIS
    run only; baking it into the saved transcript would replay stale capability
    instructions on every future resume. Assert it is absent from the on-disk
    history after a turn that had one, including the incremental snapshots.
    """
    secret = "CAP_BODY_SHOULD_NOT_PERSIST_xyz123"
    stub = _Stub({0: _tool_call_then_text_response(call_idx=0),
                  1: _sse_lines(content="cap-final")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    sid_preset = "capexclsid000000000000000000000a"
    disk_snapshots: list[list[dict]] = []

    def snapshotting_tool(args, cwd):
        # Capture the incremental on-disk history mid-turn too, so we prove the
        # capability message is excluded from EVERY persisted snapshot, not
        # only the final one.
        try:
            disk_snapshots.append(json.loads(
                runner_better_agent._session_path(sid_preset).read_text())["messages"])
        except Exception:
            disk_snapshots.append([])
        return "tool-output"

    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", snapshotting_tool)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_cap_excl_"))
        runner_better_agent._save_history(sid_preset, [])
        inputs = {
            "prompt": "do-the-thing", "images": [], "files": [], "cwd": str(tmp),
            "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-cap-excl",
            "backend_url": "", "internal_token": "",
            "capability_contexts": [
                {"name": "TestCap", "category": "capability", "content": secret},
            ],
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        assert rc == 0
        with stub.lock:
            sent = stub.requests[0]
    finally:
        stub.stop()

    # The capability context MUST have reached the model (it is live for the run)
    assert any(secret in str(m.get("content")) for m in sent), (
        "capability context was not delivered to the model this turn"
    )

    # ...but it must NOT be in any persisted snapshot, mid-flight or final.
    final_disk = json.loads(
        runner_better_agent._session_path(sid_preset).read_text())["messages"]
    all_persisted = list(disk_snapshots) + [final_disk]
    assert disk_snapshots, "Bash tool never ran; no mid-flight snapshot captured"
    for snap in all_persisted:
        assert not any(secret in str(m.get("content")) for m in snap), (
            "capability context leaked into durable history; it would replay on "
            f"every future resume: {snap}"
        )
        assert not any(
            m.get("role") == "system" and secret in str(m.get("content"))
            for m in snap
        ), snap


def test_cancel_path_persists_balanced_context(monkeypatch):
    """MUST-FIX #3a (adversarial): explicit cancel-path persistence coverage.

    A cancelled turn must still leave the context it accumulated durable AND
    resume-safe (balanced), so the next turn continues instead of starting
    blank — without ever persisting a dangling tool-call block.
    """
    stub = _Stub({0: _tool_call_then_text_response(call_idx=0)})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    sid_preset = "cancelpersist000000000000000000a"

    def cancelling_tool(args, cwd):
        (Path(os.environ["OPENAI_CANCEL_RUN_DIR"]) / "cancel").write_text(
            "", encoding="utf-8")
        return "tool-output"

    monkeypatch.setitem(runner_better_agent.TOOL_HANDLERS, "Bash", cancelling_tool)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_cancel_persist_"))
        inputs = {
            "prompt": "cancel-but-keep-context", "images": [], "files": [],
            "cwd": str(tmp), "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-cancel-persist",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        monkeypatch.setenv("OPENAI_CANCEL_RUN_DIR", str(rd))
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    assert rc == 1 and complete["error"] == "cancelled", complete
    history = json.loads(
        runner_better_agent._session_path(sid_preset).read_text())["messages"]
    assert "cancel-but-keep-context" in _user_contents(history), (
        "cancelled turn lost its prompt — next resume would be blank"
    )
    assert _no_dangling_tool_calls(history), history
    assert _no_invalid_assistant(history), history


def test_exception_path_persists_accumulated_context(monkeypatch):
    """MUST-FIX #3b (adversarial): explicit exception-path persistence.

    If the model loop raises unexpectedly mid-turn, the best-effort save in the
    except handler must still leave the already-accumulated context (at minimum
    the user prompt) durable, so the next resume is not blank.
    """
    stub = _Stub({0: _sse_lines(content="never-reached")})
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}")

    sid_preset = "excpersist0000000000000000000000a"

    async def boom(*a, **k):
        raise RuntimeError("simulated mid-turn crash")

    monkeypatch.setattr(runner_better_agent, "_one_round", boom)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="openai_exc_persist_"))
        inputs = {
            "prompt": "keep-me-on-crash", "images": [], "files": [],
            "cwd": str(tmp), "model": "stub-model", "reasoning_effort": None,
            "permission": {"mode": "bypassPermissions"}, "session_id": sid_preset,
            "mode": "native", "app_session_id": "sid-app-exc-persist",
            "backend_url": "", "internal_token": "",
        }
        rd = _make_run_dir(tmp, inputs)
        rc = asyncio.run(runner_better_agent._run(rd, inputs))
        complete = json.loads((rd / "complete.json").read_text())
    finally:
        stub.stop()

    assert rc == 1 and complete["success"] is False, complete
    assert "RuntimeError" in str(complete.get("error")), complete
    history = json.loads(
        runner_better_agent._session_path(sid_preset).read_text())["messages"]
    assert "keep-me-on-crash" in _user_contents(history), (
        "exception path lost the accumulated context — next resume would be blank"
    )
    assert _no_dangling_tool_calls(history), history


if __name__ == "__main__":
    sys.exit(0)
