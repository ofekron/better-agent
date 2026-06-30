"""Tests for the openai provider kind (BA-owned agent loop).

Two layers:
1. Deterministic (always run): provider dispatch, recovery wiring, ingestion
   version, EventEmitter Claude-shape output, and the in-process tool handlers
   (incl. path-confinement security).
2. Live integration (gated on OPENAI_API_KEY + OPENAI_BASE_URL): runs a real
   turn against the configured endpoint and asserts success + a non-empty
   assistant text event. Skipped without creds.

Uses a temp BETTER_AGENT_HOME so no real session state is touched.
"""

import asyncio
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

# Set BETTER_AGENT_HOME BEFORE importing backend modules.
_TMP_HOME = tempfile.mkdtemp(prefix="openai_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import importlib  # noqa: E402


def _mod(name):
    return importlib.import_module(name)


def test_dispatch_resolves_openai():
    provider = _mod("provider")
    cls = provider._resolve_class("openai")
    assert cls.__name__ == "OpenAIProvider", cls
    assert cls.KIND == "openai"
    assert cls.supports_fork is True
    assert cls.supports_manager_mode is True
    assert cls.supports_rewind is True
    assert cls.supports_steering is True
    assert cls.supports_reasoning_effort is True


def test_recovery_family_and_version():
    pm = _mod("provider_manifest")
    ingestion = _mod("ingestion_versions")
    assert "openai" in pm.gemini_family_kinds()
    assert ingestion.current_ingestion_version("openai") >= 1


def test_openai_permission_options_and_default():
    permission = _mod("permission")
    assert permission.permission_axes_for_kind("openai") == {
        "mode": ("default", "bypassPermissions"),
    }
    assert permission.default_permission_for_kind("openai") == {
        "mode": "bypassPermissions",
    }
    assert permission.resolve_permission("openai", {"mode": "default"}, None) == {
        "mode": "default",
    }


def test_frozen_dispatch_accepts_openai():
    app_entry = _mod("app_entry")
    mode, kind, _ = app_entry._dispatch(["--run-dir", "/tmp/x", "--runner-kind", "openai"])
    assert (mode, kind) == ("runner", "openai"), (mode, kind)


def test_event_emitter_shapes():
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as d:
        emitter = runner.EventEmitter(Path(d) / "ev.jsonl")
        emitter.set_model("glm-5.2")
        emitter.feed_text_delta("Hello ")
        emitter.feed_text_delta("world")
        emitter.close_text()
        emitter.feed_tool_call_delta(0, "call_1", "Read", '{"file_path":"a"}')
        emitter.finalize_tool_calls()
        emitter.emit_tool_result("call_1", "ok")
        emitter.close()
        lines = [json.loads(l) for l in (Path(d) / "ev.jsonl").read_text().splitlines()]
    # text deltas collapse to one uuid (rewrite-on-delta).
    text_uuids = {ln["uuid"] for ln in lines
                  if ln["type"] == "assistant"
                  and ln["message"]["content"][0].get("type") == "text"}
    assert len(text_uuids) == 1, text_uuids
    tu = [ln for ln in lines if ln["message"]["content"][0].get("type") == "tool_use"]
    assert tu and tu[-1]["message"]["content"][0]["input"] == {"file_path": "a"}
    tr = [ln for ln in lines if ln["message"]["content"][0].get("type") == "tool_result"]
    assert tr and tr[-1]["message"]["content"][0]["tool_use_id"] == "call_1"
    # parent chain advances across logical blocks.
    assert lines[-1]["parentUuid"] is not None


def test_event_emitter_buffers_tool_call_until_arguments_render():
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ev.jsonl"
        emitter = runner.EventEmitter(path)
        emitter.feed_tool_call_delta(0, "call_1", "Bash", None)
        assert path.read_text() == ""
        emitter.feed_tool_call_delta(0, None, None, '{"command":')
        assert path.read_text() == ""
        emitter.feed_tool_call_delta(0, None, None, ' "pwd"}')
        emitter.finalize_tool_calls()
        emitter.close()
        lines = [json.loads(l) for l in path.read_text().splitlines()]

    tool_uses = [
        ln for ln in lines
        if ln["message"]["content"][0].get("type") == "tool_use"
    ]
    assert len(tool_uses) == 1, tool_uses
    assert tool_uses[0]["message"]["content"][0]["input"] == {"command": "pwd"}


def test_event_emitter_flushes_no_arg_tool_on_finalize():
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ev.jsonl"
        emitter = runner.EventEmitter(path)
        emitter.feed_tool_call_delta(0, "call_1", "Read", None)
        assert path.read_text() == ""
        calls = emitter.finalize_tool_calls()
        emitter.close()
        lines = [json.loads(l) for l in path.read_text().splitlines()]

    assert calls == [{"id": "call_1", "name": "Read", "arguments": "{}"}]
    assert lines[0]["message"]["content"][0]["input"] == {}


def test_event_emitter_buffers_tool_call_until_metadata_and_arguments_render():
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ev.jsonl"
        emitter = runner.EventEmitter(path)
        emitter.feed_tool_call_delta(0, None, None, '{"command": "pwd"}')
        assert path.read_text() == ""
        emitter.feed_tool_call_delta(0, "call_1", None, None)
        assert path.read_text() == ""
        emitter.feed_tool_call_delta(0, None, "Bash", None)
        emitter.close()
        lines = [json.loads(l) for l in path.read_text().splitlines()]

    assert len(lines) == 1, lines
    tool_use = lines[0]["message"]["content"][0]
    assert tool_use["id"] == "call_1"
    assert tool_use["name"] == "Bash"
    assert tool_use["input"] == {"command": "pwd"}


def test_openai_bash_alias_input_ingests_as_canonical_command():
    runner = _mod("runner_better_agent")
    for alias in ("cmd", "shell_command"):
        with tempfile.TemporaryDirectory() as d:
            emitter = runner.EventEmitter(Path(d) / "ev.jsonl")
            emitter.feed_tool_call_delta(
                0,
                "call_bash",
                "bash",
                json.dumps({alias: "echo hi"}),
            )
            calls = emitter.finalize_tool_calls()
            emitter.close()
            lines = [
                json.loads(l)
                for l in (Path(d) / "ev.jsonl").read_text().splitlines()
            ]

        tool_use = lines[-1]["message"]["content"][0]
        assert tool_use["name"] == "Bash"
        assert tool_use["input"] == {"command": "echo hi"}
        assert calls == [{
            "id": "call_bash",
            "name": "Bash",
            "arguments": json.dumps({"command": "echo hi"}, ensure_ascii=False),
        }]


def test_openai_bash_alias_input_dispatches_canonical_command():
    runner = _mod("runner_better_agent")

    for alias in ("cmd", "shell_command"):
        seen = []

        def fake_bash(args, cwd):
            seen.append(args)
            return "ok"

        with tempfile.TemporaryDirectory() as d:
            emitter = runner.EventEmitter(Path(d) / "ev.jsonl")
            original_bash = runner.TOOL_HANDLERS.get("Bash")
            runner.TOOL_HANDLERS["Bash"] = fake_bash
            try:
                result = asyncio.run(runner._dispatch_tool(
                    {
                        "id": "call_bash",
                        "name": "Bash",
                        "arguments": json.dumps({alias: "echo hi"}),
                    },
                    Path(d),
                    "app-session",
                    Path(d),
                    True,
                    False,
                    "",
                    "",
                    emitter,
                    {},
                    runner.LockRegistry(),
                    False,
                ))
            finally:
                if original_bash is None:
                    runner.TOOL_HANDLERS.pop("Bash", None)
                else:
                    runner.TOOL_HANDLERS["Bash"] = original_bash
                emitter.close()

        assert result == "ok"
        assert seen == [{"command": "echo hi"}]


def test_openai_bash_explicit_command_wins_over_alias():
    runner = _mod("runner_better_agent")
    raw = {"cmd": "echo alias", "command": "echo canonical"}
    assert runner._canonical_tool_input("Bash", raw) == {
        "command": "echo canonical",
    }


def test_openai_bash_alias_approval_uses_canonical_command():
    runner = _mod("runner_better_agent")
    approvals = []

    def fake_approval(**kwargs):
        approvals.append(kwargs)
        return True

    def fake_bash(args, cwd):
        return "ok"

    with tempfile.TemporaryDirectory() as d:
        emitter = runner.EventEmitter(Path(d) / "ev.jsonl")
        original_approval = runner.request_tool_approval
        original_bash = runner.TOOL_HANDLERS.get("Bash")
        runner.request_tool_approval = fake_approval
        runner.TOOL_HANDLERS["Bash"] = fake_bash
        try:
            result = asyncio.run(runner._dispatch_tool(
                {
                    "id": "call_bash",
                    "name": "Bash",
                    "arguments": json.dumps({"cmd": "echo hi"}),
                },
                Path(d),
                "app-session",
                Path(d),
                False,
                True,
                "http://backend",
                "tok",
                emitter,
                {},
                runner.LockRegistry(),
                False,
            ))
        finally:
            runner.request_tool_approval = original_approval
            if original_bash is None:
                runner.TOOL_HANDLERS.pop("Bash", None)
            else:
                runner.TOOL_HANDLERS["Bash"] = original_bash
            emitter.close()

    assert result == "ok"
    assert approvals
    assert approvals[0]["tool_name"] == "Bash"
    assert approvals[0]["summary"] == {
        "tool": "Bash",
        "input": {"command": "echo hi"},
    }


def test_bash_tool_scrubs_provider_and_internal_secrets():
    runner = _mod("runner_better_agent")
    old_env = os.environ.copy()
    os.environ.update({
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "ak-secret",
        "BETTER_CLAUDE_INTERNAL_TOKEN": "bc-secret",
        "BETTER_AGENT_INTERNAL_TOKEN": "ba-secret",
        "SAFE_VISIBLE_FOR_TEST": "ok",
    })
    try:
        env = runner._tool_subprocess_env()
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in env
    assert "BETTER_AGENT_INTERNAL_TOKEN" not in env
    assert env.get("SAFE_VISIBLE_FOR_TEST") == "ok"


def test_openai_loopback_retries_disk_token_after_forbidden():
    runner = _mod("runner_better_agent")
    token_file = Path(os.environ["BETTER_AGENT_HOME"]) / "internal_token"
    token_file.write_text("disk-token", encoding="utf-8")
    runner._token_cache["token"] = None
    runner._token_cache["mtime"] = 0.0

    seen_tokens = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"success": True}).encode("utf-8")

    def fake_urlopen(req, *args, **kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == "spawn-token":
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
        return FakeResponse()

    original_urlopen = runner.urllib.request.urlopen
    try:
        runner.urllib.request.urlopen = fake_urlopen
        recovered = runner._post_loopback_sync(
            {},
            backend_url="http://127.0.0.1:9999",
            internal_token="spawn-token",
            url_path="/api/internal/ask",
            timeout_s=30.0,
        )
    finally:
        runner.urllib.request.urlopen = original_urlopen

    assert recovered == {"success": True}
    assert seen_tokens == ["spawn-token", "disk-token"]


def test_openai_loopback_recovers_completed_ask_result():
    runner = _mod("runner_better_agent")
    ask_status_store = _mod("ask_status_store")
    result = {"success": True, "assistant_content": "done"}
    ask_status_store.write_status("ask_done", result=result)

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    def fail_sleep(seconds):
        raise AssertionError("durable ask result should avoid retry sleep")

    original_urlopen = runner.urllib.request.urlopen
    original_sleep = runner.time.sleep
    try:
        runner.urllib.request.urlopen = fake_urlopen
        runner.time.sleep = fail_sleep
        recovered = runner._post_loopback_sync(
            {},
            backend_url="http://127.0.0.1:9999",
            internal_token="token",
            url_path="/api/internal/ask",
            timeout_s=runner.DELEGATE_HTTP_TIMEOUT_S,
            recover=lambda: runner._recover_ask_result("ask_done"),
        )
    finally:
        runner.urllib.request.urlopen = original_urlopen
        runner.time.sleep = original_sleep

    assert recovered == result


def test_openai_runner_exposes_and_dispatches_extension_mcp_tools():
    runner = _mod("runner_better_agent")
    original_configs = runner._extension_mcp_server_configs_for_run
    original_list = runner._mcp_list_tools
    original_call = runner._mcp_call_tool
    calls = []

    def fake_configs(inputs, *, user_facing, bare):
        assert user_facing is True
        assert bare is False
        return {
            "better-agent-requirements": {
                "command": sys.executable,
                "args": ["-c", "pass"],
                "env": {},
            },
        }

    async def fake_list(server_name, config):
        assert server_name == "better-agent-requirements"
        return [{
            "name": "get_requirements",
            "description": "Return processed requirements.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }]

    async def fake_call(spec, args):
        calls.append((spec, args))
        return json.dumps({"success": True, "count": 1})

    try:
        runner._extension_mcp_server_configs_for_run = fake_configs
        runner._mcp_list_tools = fake_list
        runner._mcp_call_tool = fake_call
        schemas, handlers = asyncio.run(runner._extension_mcp_tools_for_run(
            {"cwd": "/repo"},
            user_facing=True,
            bare=False,
            used_names=set(),
        ))
        assert [schema["function"]["name"] for schema in schemas] == ["get_requirements"]
        assert schemas[0]["function"]["parameters"]["required"] == ["query"]
        assert "get_requirements" in handlers

        with tempfile.TemporaryDirectory() as d:
            emitter = runner.EventEmitter(Path(d) / "ev.jsonl")
            result = asyncio.run(runner._dispatch_tool(
                {
                    "id": "call_req",
                    "name": "get_requirements",
                    "arguments": json.dumps({"query": "assistant requirements"}),
                },
                Path(d),
                "app-session",
                Path(d),
                True,
                True,
                "http://backend",
                "tok",
                emitter,
                {},
                runner.LockRegistry(),
                False,
                handlers,
            ))
            emitter.close()
    finally:
        runner._extension_mcp_server_configs_for_run = original_configs
        runner._mcp_list_tools = original_list
        runner._mcp_call_tool = original_call

    assert json.loads(result) == {"success": True, "count": 1}
    assert calls
    assert calls[0][0]["server_name"] == "better-agent-requirements"
    assert calls[0][0]["tool_name"] == "get_requirements"
    assert calls[0][1] == {"query": "assistant requirements"}


def test_openai_runner_speaks_real_requirements_mcp_stdio():
    runner = _mod("runner_better_agent")
    repo = Path(__file__).resolve().parents[2]
    config = {
        "command": sys.executable,
        "args": [str(repo / "better-agent-private/extensions/requirements/mcp/server.py")],
        "env": {
            "PYTHONPATH": os.pathsep.join([
                str(repo / "sdk"),
                str(repo / "backend"),
                str(repo / "better-agent-private/extensions/requirements"),
            ]),
            "BETTER_CLAUDE_BACKEND_URL": "http://127.0.0.1:9",
            "BETTER_CLAUDE_INTERNAL_TOKEN": "test-token",
            "BETTER_CLAUDE_APP_SESSION_ID": "app-session",
            "BETTER_CLAUDE_CWD": str(repo),
        },
    }

    tools = asyncio.run(runner._mcp_list_tools("better-agent-requirements", config))
    assert {tool["name"] for tool in tools} >= {
        "get_requirements",
        "get_requirements_internal",
    }

    result = asyncio.run(runner._mcp_call_tool(
        {
            "server_name": "better-agent-requirements",
            "tool_name": "get_requirements",
            "config": config,
        },
        {"query": "assistant requirements", "cwd": str(repo), "max_matches": 1},
    ))
    payload = json.loads(result)
    assert payload["success"] is False
    assert "better_agent_sdk" not in payload.get("error", "")
    assert "INTERNAL_TOKEN" not in payload.get("error", "")


def test_openai_attach_recovered_run_schedules_bootstrap():
    provider_mod = _mod("provider_openai")
    provider = provider_mod.OpenAIProvider({
        "id": "openai-test",
        "kind": "openai",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "test",
    })
    scheduled = []

    async def fake_bootstrap(rs):
        return None

    def fake_schedule(loop, coro, *, name):
        scheduled.append((loop, coro, name))
        coro.close()

    original_schedule = provider_mod.schedule_loop_task
    original_bootstrap = provider._bootstrap_run
    try:
        provider_mod.schedule_loop_task = fake_schedule
        provider._bootstrap_run = fake_bootstrap
        queue = asyncio.Queue()
        ok = provider.attach_recovered_run(
            desc={
                "run_id": "openai-live-restart",
                "pid": os.getpid(),
                "mode": "native",
                "app_session_id": "app-session",
                "persist_to": "app-session",
                "session_id": "openai-session",
                "processed_line": 7,
                "target_message_id": "msg-1",
                "turn_run_id": "turn-1",
            },
            queue=queue,
            loop=asyncio.new_event_loop(),
        )
    finally:
        provider_mod.schedule_loop_task = original_schedule
        provider._bootstrap_run = original_bootstrap
        if scheduled:
            scheduled[0][0].close()

    assert ok is True
    rs = provider._runs["openai-live-restart"]
    assert rs.popen.recovered_stub is True
    assert rs.processed_line == 7
    assert rs.queue is queue
    assert rs.target_message_id == "msg-1"
    assert scheduled and scheduled[0][2].startswith("openai-recover-bootstrap-")


def test_tools_path_confinement():
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as cwd:
        cwdp = Path(cwd)
        (cwdp / "in.txt").write_text("ok", encoding="utf-8")
        # read inside cwd works
        assert "ok" in runner._tool_read({"file_path": "in.txt"}, cwdp)
        # traversal escape rejected
        res = runner._tool_read({"file_path": "../../etc/passwd"}, cwdp)
        assert res.startswith("Error:"), res
        # write escape rejected
        res = runner._tool_write({"file_path": "/tmp/escape_openai_test.txt",
                                  "content": "x"}, cwdp)
        assert res.startswith("Error:"), res
        # edit replace_all guard
        (cwdp / "d.txt").write_text("a\na\n", encoding="utf-8")
        res = runner._tool_edit({"file_path": "d.txt", "old_string": "a",
                                 "new_string": "b"}, cwdp)
        assert "matches 2" in res, res
        # grep
        (cwdp / "g.txt").write_text("foo bar\nbaz\n", encoding="utf-8")
        assert "foo bar" in runner._tool_grep({"pattern": "foo"}, cwdp)


def test_read_allows_declared_runtime_skill_files_only():
    runner = _mod("runner_better_agent")
    runtime_skills = _mod("runtime_skills")
    original_discover = runtime_skills._discover_skills
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as skill_root:
        cwdp = Path(cwd)
        skill_dir = Path(skill_root) / "project-structure"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("declared skill", encoding="utf-8")
        other = Path(skill_root) / "other.txt"
        other.write_text("secret", encoding="utf-8")

        def fake_discover(_cwd):
            return [{
                "name": "project-structure",
                "description": "",
                "dir": str(skill_dir),
                "path": str(skill_md),
            }]

        try:
            runtime_skills._discover_skills = fake_discover
            assert "declared skill" in runner._tool_read({"file_path": str(skill_md)}, cwdp)
            assert runner._tool_read({"file_path": str(other)}, cwdp).startswith("Error:")
            assert runner._tool_write({
                "file_path": str(skill_md),
                "content": "mutate",
            }, cwdp).startswith("Error:")
        finally:
            runtime_skills._discover_skills = original_discover


def test_live_turn_against_endpoint():
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        print("skip live openai test (no OPENAI_API_KEY/OPENAI_BASE_URL)")
        return
    runner = _mod("runner_better_agent")
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as rd:
        (Path(cwd) / "hello.txt").write_text("openai-loop-works", encoding="utf-8")
        inputs = {"prompt": "Use the Read tool to read hello.txt then reply ONLY with its contents.",
                  "cwd": cwd, "model": os.environ.get("OPENAI_MODEL", "glm-5.2"),
                  "mode": "native", "app_session_id": "test-live",
                  "session_id": None, "permission": {"bash": "bypassPermissions"},
                  "images": [], "files": [], "reasoning_effort": None,
                  "disallowed_tools": [], "setting_sources": [], "backend_url": "",
                  "internal_token": "", "fork": False, "supervised": False,
                  "supervisor_agent_session_id": None, "worker_agent_session_id": None,
                  "mssg_sender_session_id": None, "browser_harness_enabled": False,
                  "open_file_panel_enabled": False, "bare_config": False,
                  "working_mode": None, "worker_working_mode": None,
                  "context_strategy": "", "continuation_chain": [],
                  "provider_run_config": {}, "capability_contexts": [],
                  "target_message_id": None, "turn_run_id": None,
                  "disabled_builtin_tools": [], "disabled_builtin_extensions": []}
        (Path(rd) / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
        import asyncio
        rc = runner.main(Path(rd))
        complete = json.loads((Path(rd) / "complete.json").read_text())
        assert rc == 0, complete
        assert complete["success"] is True, complete
        lines = [json.loads(l) for l in (Path(rd) / "session_events.jsonl").read_text().splitlines()]
        texts = [ln["message"]["content"][0]["text"] for ln in lines
                 if ln["type"] == "assistant"
                 and ln["message"]["content"][0].get("type") == "text"]
        assert texts, "no assistant text emitted"
        assert "openai-loop-works" in "".join(texts), "".join(texts)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
