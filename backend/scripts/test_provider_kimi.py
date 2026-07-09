"""Focused tests for the Kimi CLI provider.

Pins:
  1. Class contract: KimiProvider subclasses GeminiProvider, KIND="kimi",
     native-only capability matrix (no fork / team / steering / reasoning),
     simulated rewind on.
  2. Env hygiene: build_env clears Claude/Anthropic vars.
  3. Models: static KIMI_MODELS seed + `fetch_kimi_models` parses the
     config.toml [models] table (default_model first, [] on failure).
  4. Runner argv: prompt never in argv, --session always present,
     resume reuses the given sid.
  5. Runner normalization: kosong-Message stream-json lines map to the
     correct Claude-shaped agent_message events (assistant text/thinking,
     tool_use with parsed JSON arguments + tool/key mapping, tool_result
     with is_error detection), deterministic render uuids, user/system
     lines skipped.
  6. Runner end-to-end against a fake `kimi` binary: state.json carries a
     pre-generated session id, session_events.jsonl holds the normalized
     events, complete.json reports success; a failing binary yields
     success=False with the plain-text error captured.

Run:
    cd backend && .venv/bin/python scripts/test_provider_kimi.py
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
_TMP_HOME = _test_home.isolate("bc-test-provider-kimi-")

import provider_kimi  # noqa: E402
import runner_kimi  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402


SID = "11111111-2222-3333-4444-555555555555"


def test_class_contract() -> bool:
    cls = provider_kimi.KimiProvider
    return issubclass(cls, GeminiProvider) and cls.KIND == "kimi"


def test_capability_matrix() -> bool:
    cls = provider_kimi.KimiProvider
    expected = {
        "supports_fork": False,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": False,
    }
    return all(getattr(cls, k) is v for k, v in expected.items())


def test_build_env_clears_anthropic() -> bool:
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-should-be-cleared")
    try:
        inst = provider_kimi.KimiProvider({"id": "k1", "kind": "kimi", "mode": "api_key"})
        env = inst.build_env()
        return not any(k in env for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
        ))
    finally:
        if os.environ.get("ANTHROPIC_API_KEY") == "test-key-should-be-cleared":
            del os.environ["ANTHROPIC_API_KEY"]


def test_models_static_seed() -> bool:
    return (
        "kimi-code/kimi-for-coding" in provider_kimi.KIMI_MODELS
        and any(m.startswith("kimi-k2") for m in provider_kimi.KIMI_MODELS)
    )


def test_fetch_kimi_models_parses_config() -> bool:
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text(
            'default_model = "kimi-k2-thinking"\n'
            '\n'
            '[models."kimi-code/kimi-for-coding"]\n'
            'model = "kimi-for-coding"\n'
            '\n'
            '[models."kimi-k2-thinking"]\n'
            'model = "kimi-k2-thinking"\n',
            encoding="utf-8",
        )
        parsed = provider_kimi.fetch_kimi_models(cfg)
    return parsed == ["kimi-k2-thinking", "kimi-code/kimi-for-coding"]


def test_fetch_kimi_models_fails_closed() -> bool:
    with tempfile.TemporaryDirectory() as td:
        missing = provider_kimi.fetch_kimi_models(Path(td) / "nope.toml")
        bad = Path(td) / "bad.toml"
        bad.write_text("not [ valid toml", encoding="utf-8")
        malformed = provider_kimi.fetch_kimi_models(bad)
        empty = Path(td) / "empty.toml"
        empty.write_text('default_model = ""\n', encoding="utf-8")
        no_models = provider_kimi.fetch_kimi_models(empty)
    return missing == [] and malformed == [] and no_models == []


def test_argv_never_carries_prompt() -> bool:
    argv = runner_kimi.build_kimi_argv(
        "/usr/bin/kimi", model="kimi-k2-thinking", session_id=SID, cwd="/tmp/proj",
    )
    return (
        "--print" in argv
        and argv[argv.index("--output-format") + 1] == "stream-json"
        and argv[argv.index("--session") + 1] == SID
        and argv[argv.index("--work-dir") + 1] == "/tmp/proj"
        and argv[argv.index("--model") + 1] == "kimi-k2-thinking"
        and not any("prompt" in a.lower() or "--command" in a for a in argv)
    )


def test_argv_omits_model_when_unset() -> bool:
    argv = runner_kimi.build_kimi_argv("/usr/bin/kimi", model="", session_id=SID, cwd="/tmp")
    return "--model" not in argv


def test_normalizes_assistant_string_content() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "assistant", "content": "hello"},
        session_id=SID, parent_uuid=SID, model="kimi-k2-thinking", event_key="0",
    )
    if len(out) != 1 or out[0]["type"] != "agent_message":
        return False
    data = out[0]["data"]
    block = data["message"]["content"][0]
    return (
        data["type"] == "assistant"
        and data["parentUuid"] == SID
        and data["message"]["model"] == "kimi-k2-thinking"
        and block == {"type": "text", "text": "hello"}
    )


def test_normalizes_think_and_text_parts() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "assistant", "content": [
            {"type": "think", "think": "hmm"},
            {"type": "text", "text": "answer"},
        ]},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="1",
    )
    if len(out) != 1:
        return False
    content = out[0]["data"]["message"]["content"]
    return content == [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "answer"},
    ]


def test_normalizes_tool_calls() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "assistant", "content": [], "tool_calls": [
            {"type": "function", "id": "call_1",
             "function": {"name": "Shell", "arguments": '{"command": "ls"}'}},
            {"type": "function", "id": "call_2",
             "function": {"name": "ReadFile", "arguments": '{"path": "/tmp/a.py"}'}},
        ]},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="2",
    )
    if len(out) != 1:
        return False
    blocks = out[0]["data"]["message"]["content"]
    return (
        blocks[0] == {"type": "tool_use", "id": "call_1", "name": "Bash",
                      "input": {"command": "ls"}}
        and blocks[1] == {"type": "tool_use", "id": "call_2", "name": "Read",
                          "input": {"file_path": "/tmp/a.py"}}
    )


def test_tool_call_bad_arguments_wrapped() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "assistant", "content": [], "tool_calls": [
            {"type": "function", "id": "c",
             "function": {"name": "Shell", "arguments": "not json {"}},
        ]},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="3",
    )
    return out[0]["data"]["message"]["content"][0]["input"] == {"input": "not json {"}


def test_normalizes_tool_result() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "tool", "content": "file.py", "tool_call_id": "call_1"},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="4",
    )
    if len(out) != 1:
        return False
    data = out[0]["data"]
    block = data["message"]["content"][0]
    return (
        data["type"] == "user"
        and block["type"] == "tool_result"
        and block["tool_use_id"] == "call_1"
        and block["content"] == "file.py"
        and block["is_error"] is False
    )


def test_tool_result_error_detection() -> bool:
    out = runner_kimi.normalize_kimi_message(
        {"role": "tool",
         "content": [{"type": "text", "text": "<system>ERROR: boom</system>"}],
         "tool_call_id": "call_1"},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="5",
    )
    return out[0]["data"]["message"]["content"][0]["is_error"] is True


def test_uuid_is_deterministic() -> bool:
    kwargs = dict(session_id=SID, parent_uuid=SID, model="kimi", event_key="7")
    a = runner_kimi.normalize_kimi_message({"role": "assistant", "content": "x"}, **kwargs)
    b = runner_kimi.normalize_kimi_message({"role": "assistant", "content": "x"}, **kwargs)
    c = runner_kimi.normalize_kimi_message(
        {"role": "assistant", "content": "x"},
        session_id=SID, parent_uuid=SID, model="kimi", event_key="8",
    )
    return (
        a[0]["data"]["uuid"] == b[0]["data"]["uuid"]
        and a[0]["data"]["uuid"] != c[0]["data"]["uuid"]
    )


def test_skips_user_and_system_roles() -> bool:
    for role in ("user", "system"):
        out = runner_kimi.normalize_kimi_message(
            {"role": role, "content": "echo"},
            session_id=SID, parent_uuid=SID, model="kimi", event_key="9",
        )
        if out != []:
            return False
    return True


def test_capability_context_labels_team_message() -> bool:
    prompt = runner_kimi._prepend_capability_context("<mssg>done</mssg>", {
        "source": "mssg",
        "capability_contexts": [{
            "name": "Runtime",
            "category": "system",
            "content": "Use runtime context.",
        }],
    })
    return (
        "## Message\n\n<mssg>" in prompt
        and "## User prompt\n\n<mssg>" not in prompt
    )


def _write_fake_kimi(bin_dir: Path, script_body: str) -> None:
    fake = bin_dir / "kimi"
    fake.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_runner_with_fake_kimi(script_body: str, inputs: dict) -> tuple[int, Path]:
    td = Path(tempfile.mkdtemp(prefix="kimi-runner-test-"))
    bin_dir = td / "bin"
    bin_dir.mkdir()
    _write_fake_kimi(bin_dir, script_body)
    run_dir = td / "run"
    run_dir.mkdir()
    (run_dir / "input.json").write_text(json.dumps(inputs), encoding="utf-8")
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        rc = runner_kimi.main(run_dir)
    finally:
        os.environ["PATH"] = old_path
    return rc, run_dir


def test_runner_end_to_end_success() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = (
            "cat > /dev/null\n"
            "echo '"
            + json.dumps({
                "role": "assistant",
                "content": "on it",
                "tool_calls": [{
                    "type": "function", "id": "call_1",
                    "function": {"name": "Shell", "arguments": "{\"command\": \"ls\"}"},
                }],
            })
            + "'\n"
            "echo '"
            + json.dumps({"role": "tool", "content": "file.py", "tool_call_id": "call_1"})
            + "'\n"
            "echo '"
            + json.dumps({"role": "assistant", "content": "done"})
            + "'\n"
            "exit 0\n"
        )
        rc, run_dir = _run_runner_with_fake_kimi(
            script, {"prompt": "list files", "cwd": cwd, "app_session_id": "app1"},
        )
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        events = [
            json.loads(line)
            for line in (run_dir / "session_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        shutil.rmtree(run_dir.parent, ignore_errors=True)

    sid = state.get("session_id") or ""
    try:
        uuid_mod.UUID(sid)
    except ValueError:
        return False
    if rc != 0 or complete["success"] is not True or complete["session_id"] != sid:
        return False
    if len(events) != 3 or any(e["type"] != "agent_message" for e in events):
        return False
    kinds = [e["data"]["message"]["content"][0]["type"] for e in events]
    # parentUuid threads: second event's parent is the first event's uuid.
    return (
        kinds == ["text", "tool_result", "text"]
        and events[1]["data"]["parentUuid"] == events[0]["data"]["uuid"]
        and events[0]["data"]["parentUuid"] == sid
    )


def test_runner_resume_reuses_session_id() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = (
            "cat > /dev/null\n"
            "echo '" + json.dumps({"role": "assistant", "content": "resumed"}) + "'\n"
            "exit 0\n"
        )
        rc, run_dir = _run_runner_with_fake_kimi(
            script,
            {"prompt": "hi again", "cwd": cwd, "session_id": SID, "app_session_id": "app1"},
        )
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 0 and state["session_id"] == SID and complete["session_id"] == SID


def test_runner_failure_captures_plain_text_error() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = (
            "cat > /dev/null\n"
            "echo 'Error code: 402 - membership inactive'\n"
            "exit 1\n"
        )
        rc, run_dir = _run_runner_with_fake_kimi(
            script, {"prompt": "hello", "cwd": cwd, "app_session_id": "app1"},
        )
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        events = [
            json.loads(line)
            for line in (run_dir / "session_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return (
        rc == 1
        and complete["success"] is False
        and "402" in (complete["error"] or "")
        and events
        and events[-1]["data"].get("isApiErrorMessage") is True
    )


def test_runner_ghost_completion_fails() -> bool:
    with tempfile.TemporaryDirectory() as cwd:
        script = "cat > /dev/null\nexit 0\n"
        rc, run_dir = _run_runner_with_fake_kimi(
            script, {"prompt": "hello", "cwd": cwd, "app_session_id": "app1"},
        )
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        shutil.rmtree(run_dir.parent, ignore_errors=True)
    return rc == 1 and complete["success"] is False and bool(complete["error"])


TESTS = [
    ("class_contract", test_class_contract),
    ("capability_matrix", test_capability_matrix),
    ("build_env_clears_anthropic", test_build_env_clears_anthropic),
    ("models_static_seed", test_models_static_seed),
    ("fetch_kimi_models_parses_config", test_fetch_kimi_models_parses_config),
    ("fetch_kimi_models_fails_closed", test_fetch_kimi_models_fails_closed),
    ("argv_never_carries_prompt", test_argv_never_carries_prompt),
    ("argv_omits_model_when_unset", test_argv_omits_model_when_unset),
    ("normalizes_assistant_string_content", test_normalizes_assistant_string_content),
    ("normalizes_think_and_text_parts", test_normalizes_think_and_text_parts),
    ("normalizes_tool_calls", test_normalizes_tool_calls),
    ("tool_call_bad_arguments_wrapped", test_tool_call_bad_arguments_wrapped),
    ("normalizes_tool_result", test_normalizes_tool_result),
    ("tool_result_error_detection", test_tool_result_error_detection),
    ("uuid_is_deterministic", test_uuid_is_deterministic),
    ("skips_user_and_system_roles", test_skips_user_and_system_roles),
    ("capability_context_labels_team_message", test_capability_context_labels_team_message),
    ("runner_end_to_end_success", test_runner_end_to_end_success),
    ("runner_resume_reuses_session_id", test_runner_resume_reuses_session_id),
    ("runner_failure_captures_plain_text_error", test_runner_failure_captures_plain_text_error),
    ("runner_ghost_completion_fails", test_runner_ghost_completion_fails),
]


def main() -> int:
    failures = []
    try:
        for name, fn in TESTS:
            ok = fn()
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
