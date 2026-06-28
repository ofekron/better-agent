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

import json
import os
import sys
import tempfile
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
    runner = _mod("runner_openai")
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


def test_bash_tool_scrubs_provider_and_internal_secrets():
    runner = _mod("runner_openai")
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


def test_tools_path_confinement():
    runner = _mod("runner_openai")
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


def test_live_turn_against_endpoint():
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        print("skip live openai test (no OPENAI_API_KEY/OPENAI_BASE_URL)")
        return
    runner = _mod("runner_openai")
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
