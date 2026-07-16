"""Focused tests for the Grok Build CLI provider.

Pins:
  1. Class contract: GrokProvider subclasses GeminiProvider, KIND="grok",
     fork/reasoning-effort ARE supported (unlike kimi/qwen), no
     manager-mode/steering/native-subagents.
  2. Env hygiene: build_env clears foreign-provider vars, sets
     GROK_DISABLE_AUTOUPDATER, routes api_key mode through XAI_API_KEY.
  3. Models: static GROK_MODELS seed; `fetch_grok_models` parses
     `grok models` output, [] on any failure.
  4. Runner argv: prompt never in argv (--prompt-file only); fresh runs
     pass -s <uuid>; resume passes -r <sid>; fork passes -r <sid>
     --fork-session; --yolo and --no-auto-update always present.
  5. Runner normalization: text/thought deltas accumulate into a single
     assistant message that keeps the SAME uuid across deltas (in-place
     rewrite semantics) and gets separate uuids across turns.
  6. Runner end-to-end against a fake `grok` binary: state.json carries
     the pre-generated session id for a fresh run, session_events.jsonl
     holds the accumulated assistant event, complete.json reports
     success; an `error` event yields success=False with that message.

Run:
    cd backend && .venv/bin/python scripts/test_provider_grok.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import uuid as uuid_mod
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-grok-")

import provider_grok  # noqa: E402
import runner_grok  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402


SID = "11111111-2222-3333-4444-555555555555"


def test_class_contract() -> bool:
    cls = provider_grok.GrokProvider
    return issubclass(cls, GeminiProvider) and cls.KIND == "grok"


def test_capability_matrix() -> bool:
    cls = provider_grok.GrokProvider
    expected = {
        "supports_fork": True,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": True,
    }
    return all(getattr(cls, k) is v for k, v in expected.items())


def test_reasoning_effort_options() -> bool:
    cls = provider_grok.GrokProvider
    return (
        "xhigh" in cls.reasoning_effort_options
        and "max" in cls.reasoning_effort_options
        and cls.default_reasoning_effort in cls.reasoning_effort_options
    )


def test_build_env_clears_foreign_and_routes_api_key() -> bool:
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-should-be-cleared")
    try:
        inst = provider_grok.GrokProvider({
            "id": "g1", "kind": "grok", "mode": "api_key", "api_key": "xai-secret",
        })
        env = inst.build_env()
        return (
            not any(k in env for k in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CONFIG_DIR", "GEMINI_API_KEY", "CODEX_HOME",
            ))
            and env.get("XAI_API_KEY") == "xai-secret"
            and env.get("GROK_DISABLE_AUTOUPDATER") == "1"
        )
    finally:
        if os.environ.get("ANTHROPIC_API_KEY") == "test-key-should-be-cleared":
            del os.environ["ANTHROPIC_API_KEY"]


def test_models_static_seed() -> bool:
    return "grok-build" in provider_grok.GROK_MODELS


def test_fetch_grok_models_missing_cli_fails_closed() -> bool:
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = ""
    try:
        return provider_grok.fetch_grok_models() == []
    finally:
        os.environ["PATH"] = old_path


# ============================================================================
# Runner argv
# ============================================================================
def test_argv_never_carries_prompt() -> bool:
    prompt_file = Path("/tmp/does-not-matter/prompt.txt")
    argv = runner_grok.build_grok_argv(
        "/usr/bin/grok", model="grok-build", reasoning_effort="high",
        cwd="/tmp/proj", prompt_file=prompt_file, resume_session_id=SID,
    )
    prompt_file_flag_idx = argv.index("--prompt-file")
    prompt_file_value_idx = prompt_file_flag_idx + 1
    other_args = [a for i, a in enumerate(argv) if i not in (prompt_file_flag_idx, prompt_file_value_idx)]
    return (
        argv[prompt_file_value_idx] == str(prompt_file)
        and not any("prompt" in a.lower() for a in other_args)
        and "-p" not in argv
    )


def test_argv_fresh_run_creates_session() -> bool:
    argv = runner_grok.build_grok_argv(
        "/usr/bin/grok", model="", reasoning_effort="", cwd="/tmp",
        prompt_file=Path("/tmp/p.txt"), create_session_id="new-sid",
    )
    return (
        argv[argv.index("-s") + 1] == "new-sid"
        and "-r" not in argv
        and "--fork-session" not in argv
    )


def test_argv_resume_uses_dash_r() -> bool:
    argv = runner_grok.build_grok_argv(
        "/usr/bin/grok", model="", reasoning_effort="", cwd="/tmp",
        prompt_file=Path("/tmp/p.txt"), resume_session_id=SID,
    )
    return argv[argv.index("-r") + 1] == SID and "-s" not in argv


def test_argv_fork_adds_fork_session_flag() -> bool:
    argv = runner_grok.build_grok_argv(
        "/usr/bin/grok", model="", reasoning_effort="", cwd="/tmp",
        prompt_file=Path("/tmp/p.txt"), resume_session_id=SID, fork=True,
    )
    return (
        argv[argv.index("-r") + 1] == SID
        and "--fork-session" in argv
    )


def test_argv_always_yolo_and_no_auto_update() -> bool:
    argv = runner_grok.build_grok_argv(
        "/usr/bin/grok", model="", reasoning_effort="", cwd="/tmp",
        prompt_file=Path("/tmp/p.txt"),
    )
    return "--yolo" in argv and "--no-auto-update" in argv


# ============================================================================
# Event normalization
# ============================================================================
def test_assistant_event_accumulates_text_deltas() -> bool:
    first = runner_grok._assistant_event(
        text_buf="Here's", thought_buf="", uuid_str="u1", parent_uuid=SID, model="grok-build",
    )
    second = runner_grok._assistant_event(
        text_buf="Here's a summary", thought_buf="", uuid_str="u1", parent_uuid=SID, model="grok-build",
    )
    return (
        first["uuid"] == second["uuid"] == "u1"
        and first["message"]["content"][0]["text"] == "Here's"
        and second["message"]["content"][0]["text"] == "Here's a summary"
    )


def test_assistant_event_includes_thinking_block() -> bool:
    ev = runner_grok._assistant_event(
        text_buf="answer", thought_buf="reasoning...", uuid_str="u2", parent_uuid=SID, model="grok-build",
    )
    blocks = ev["message"]["content"]
    return (
        blocks[0] == {"type": "thinking", "thinking": "reasoning..."}
        and blocks[1] == {"type": "text", "text": "answer"}
    )


def test_assistant_event_empty_returns_none() -> bool:
    return runner_grok._assistant_event(
        text_buf="", thought_buf="", uuid_str="u3", parent_uuid=SID, model="grok-build",
    ) is None


def test_usage_from_grok_event() -> bool:
    usage = runner_grok.usage_from_grok_event({
        "usage": {
            "input_tokens": 100, "cache_read_input_tokens": 50,
            "output_tokens": 20, "reasoning_tokens": 5, "total_tokens": 170,
        },
        "total_cost_usd": 0.01,
    })
    return (
        usage["input_tokens"] == 100
        and usage["cache_read_input_tokens"] == 50
        and usage["output_tokens"] == 20
        and usage["reasoning_tokens"] == 5
        and usage["total_tokens"] == 170
        and usage["total_cost_usd"] == 0.01
    )


# ============================================================================
# Runner end-to-end against a fake `grok` binary
# ============================================================================
def _write_fake_grok(bin_dir: Path, script_body: str, *, shebang: str = "#!/bin/sh") -> None:
    fake = bin_dir / "grok"
    fake.write_text(shebang + "\n" + script_body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_runner_with_fake_grok(
    script_body: str, inputs: dict, *, shebang: str = "#!/bin/sh",
) -> tuple[int, Path]:
    import asyncio
    td = Path(tempfile.mkdtemp(prefix="grok-runner-test-"))
    bin_dir = td / "bin"
    bin_dir.mkdir()
    _write_fake_grok(bin_dir, script_body, shebang=shebang)
    run_dir = td / "run"
    run_dir.mkdir()
    (run_dir / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        rc = asyncio.run(runner_grok._run(run_dir, inputs))
    finally:
        os.environ["PATH"] = old_path
    return rc, run_dir


_PYTHON_ECHO_SID_SCRIPT = (
    "import sys, json\n"
    "argv = sys.argv[1:]\n"
    "sid = argv[argv.index('-s') + 1]\n"
    "print(json.dumps({'type': 'text', 'data': 'hi'}))\n"
    "print(json.dumps({'type': 'end', 'stopReason': 'EndTurn', 'sessionId': sid}))\n"
)


def test_runner_fresh_run_pregenerates_session_id() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        rc, run_dir = _run_runner_with_fake_grok(
            _PYTHON_ECHO_SID_SCRIPT,
            {"prompt": "hello", "cwd": cwd, "app_session_id": "app1"},
            shebang=f"#!{sys.executable}",
        )
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    try:
        uuid_mod.UUID(state.get("session_id") or "")
    except ValueError:
        return False
    return rc == 0 and complete["success"] is True and complete["session_id"] == state["session_id"]


def test_runner_accumulates_text_into_single_event() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = (
            "echo '{\"type\":\"text\",\"data\":\"Here'\"'\"'s\"}'\n"
            "echo '{\"type\":\"text\",\"data\":\" a summary\"}'\n"
            "echo '{\"type\":\"end\",\"stopReason\":\"EndTurn\",\"sessionId\":\"" + SID + "\"}'\n"
        )
        rc, run_dir = _run_runner_with_fake_grok(
            script, {"prompt": "hi", "cwd": cwd, "app_session_id": "app1"},
        )
        events = [
            json.loads(line)
            for line in (run_dir / "session_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    uuids = {e["uuid"] for e in events}
    return (
        rc == 0
        and len(events) == 2
        and len(uuids) == 1
        and events[-1]["message"]["content"][0]["text"] == "Here's a summary"
    )


def test_runner_resume_reuses_session_id() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = (
            "echo '{\"type\":\"text\",\"data\":\"resumed\"}'\n"
            "echo '{\"type\":\"end\",\"stopReason\":\"EndTurn\",\"sessionId\":\"" + SID + "\"}'\n"
        )
        rc, run_dir = _run_runner_with_fake_grok(
            script,
            {"prompt": "hi again", "cwd": cwd, "session_id": SID, "app_session_id": "app1"},
        )
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 0 and state["session_id"] == SID and complete["session_id"] == SID


def test_runner_error_event_fails_run() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = "echo '{\"type\":\"error\",\"message\":\"Not signed in.\"}'\n"
        rc, run_dir = _run_runner_with_fake_grok(
            script, {"prompt": "hello", "cwd": cwd, "app_session_id": "app1"},
        )
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 1 and complete["success"] is False and "Not signed in." in (complete["error"] or "")


def test_runner_fork_without_session_id_fails() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        rc, run_dir = _run_runner_with_fake_grok(
            "exit 0\n",
            {"prompt": "hi", "cwd": cwd, "app_session_id": "app1", "fork": True},
        )
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 1 and complete["success"] is False and "fork" in (complete["error"] or "").lower()


def test_runner_ghost_completion_fails() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        rc, run_dir = _run_runner_with_fake_grok(
            "exit 0\n", {"prompt": "hello", "cwd": cwd, "app_session_id": "app1"},
        )
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 1 and complete["success"] is False and bool(complete["error"])


TESTS = [
    ("class_contract", test_class_contract),
    ("capability_matrix", test_capability_matrix),
    ("reasoning_effort_options", test_reasoning_effort_options),
    ("build_env_clears_foreign_and_routes_api_key", test_build_env_clears_foreign_and_routes_api_key),
    ("models_static_seed", test_models_static_seed),
    ("fetch_grok_models_missing_cli_fails_closed", test_fetch_grok_models_missing_cli_fails_closed),
    ("argv_never_carries_prompt", test_argv_never_carries_prompt),
    ("argv_fresh_run_creates_session", test_argv_fresh_run_creates_session),
    ("argv_resume_uses_dash_r", test_argv_resume_uses_dash_r),
    ("argv_fork_adds_fork_session_flag", test_argv_fork_adds_fork_session_flag),
    ("argv_always_yolo_and_no_auto_update", test_argv_always_yolo_and_no_auto_update),
    ("assistant_event_accumulates_text_deltas", test_assistant_event_accumulates_text_deltas),
    ("assistant_event_includes_thinking_block", test_assistant_event_includes_thinking_block),
    ("assistant_event_empty_returns_none", test_assistant_event_empty_returns_none),
    ("usage_from_grok_event", test_usage_from_grok_event),
    ("runner_fresh_run_pregenerates_session_id", test_runner_fresh_run_pregenerates_session_id),
    ("runner_accumulates_text_into_single_event", test_runner_accumulates_text_into_single_event),
    ("runner_resume_reuses_session_id", test_runner_resume_reuses_session_id),
    ("runner_error_event_fails_run", test_runner_error_event_fails_run),
    ("runner_fork_without_session_id_fails", test_runner_fork_without_session_id_fails),
    ("runner_ghost_completion_fails", test_runner_ghost_completion_fails),
]


def main() -> int:
    failures = []
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL: {name} (exception: {type(exc).__name__}: {exc})")
                failures.append(name)
                continue
            print(("PASS" if ok else "FAIL") + f": {name}")
            if not ok:
                failures.append(name)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
