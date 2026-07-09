"""Focused tests for the Cursor provider.

Pins:
  1. Class identity: CursorProvider subclasses GeminiProvider, KIND="cursor".
  2. Capability matrix: native-only (no fork / team / steering / reasoning),
     simulated rewind on.
  3. Models: static cold-start seed + `cursor-agent models` output parsing.
  4. Permission (fail closed): only the explicit "force" mode maps to
     `-f/--force`; everything else runs the CLI default.
  5. Runner argv: headless stream-json flags, `--` prompt separation (no
     argv injection), resume/model wiring.
  6. Runner event normalization: each cursor stream-json event type maps to
     the correct Claude shape (assistant delta accumulation on a stable
     uuid, thinking, tool_use/tool_result from the proto-JSON tool_call
     oneof, result bookkeeping) and tool-call uuids are deterministic.
  7. Runner fail-closed completion on empty prompt (no CLI spawn).

Run:
    cd backend && .venv/bin/python scripts/test_provider_cursor.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-cursor-")

import provider_cursor  # noqa: E402
import runner_cursor  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402


def test_class_identity() -> bool:
    cls = provider_cursor.CursorProvider
    return issubclass(cls, GeminiProvider) and cls.KIND == "cursor"


def test_capability_matrix() -> bool:
    cls = provider_cursor.CursorProvider
    expected = {
        "supports_fork": False,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": False,
        "supports_headless_no_tools": False,
    }
    return all(getattr(cls, k) is v for k, v in expected.items())


def test_build_env_clears_claude_keeps_cursor_key() -> bool:
    import os
    os.environ["CURSOR_API_KEY"] = "test-cursor-key"
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    try:
        inst = provider_cursor.CursorProvider(
            {"id": "c1", "kind": "cursor", "mode": "subscription"}
        )
        env = inst.build_env()
        cleared = not any(k in env for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
        ))
        return cleared and env.get("CURSOR_API_KEY") == "test-cursor-key"
    finally:
        os.environ.pop("CURSOR_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_models_static_seed() -> bool:
    seed = provider_cursor.CURSOR_MODELS
    return bool(seed) and "auto" in seed and "gpt-5" in seed and "sonnet-4" in seed


def test_parses_models_output() -> bool:
    sample = (
        "\x1b[1mAvailable models:\x1b[0m\n"
        "  • auto\n"
        "  • composer-1 (default)\n"
        "  - gpt-5\n"
        "  > sonnet-4.5-thinking\n"
        "\n"
        "Usage: pick one with --model\n"
    )
    return provider_cursor.parse_cursor_models_output(sample) == [
        "auto", "composer-1", "gpt-5", "sonnet-4.5-thinking",
    ]


def test_models_output_parse_fails_closed() -> bool:
    # Fewer than 2 parseable ids → treat as parse failure (keep prior cache).
    return provider_cursor.parse_cursor_models_output("Error: not authenticated\n") == []


def test_fetch_models_returns_list() -> bool:
    if not shutil.which("cursor-agent"):
        return True
    parsed = provider_cursor.fetch_cursor_models()
    # Auth-blocked CLIs return [] (fail closed); authenticated ones return ids.
    return isinstance(parsed, list)


def test_permission_argv_fail_closed() -> bool:
    cases = [
        ({"mode": "force"}, ["-f"]),
        ({"mode": "default"}, []),
        ({"mode": "yolo"}, []),          # unknown value → closed
        ({}, []),
        (None, []),
        ("force", []),                    # non-dict → closed
    ]
    return all(runner_cursor.permission_argv(v) == out for v, out in cases)


def test_resolve_permission_fail_closed() -> bool:
    import permission as perm
    return (
        perm.resolve_permission("cursor", {"mode": "force"}, None) == {"mode": "force"}
        and perm.resolve_permission("cursor", {"mode": "default"}, None) == {"mode": "default"}
        # unknown value falls back to the axis default (full-bypass parity)
        and perm.resolve_permission("cursor", {"mode": "bypassPermissions"}, None)
        == {"mode": "force"}
        and perm.resolve_permission("cursor", None, None) == {"mode": "force"}
        and perm.resolve_permission("cursor", None, {"mode": "default"}) == {"mode": "default"}
    )


def test_build_argv_shape() -> bool:
    argv = runner_cursor.build_argv(
        cursor_bin="/bin/cursor-agent",
        prompt="--resume sneaky prompt",
        model="gpt-5",
        session_id="chat-123",
        permission={"mode": "force"},
    )
    if argv[0] != "/bin/cursor-agent":
        return False
    for flag in ("--print", "--output-format", "stream-json", "--stream-partial-output", "-f"):
        if flag not in argv:
            return False
    sep = argv.index("--")
    return (
        argv[argv.index("--model") + 1] == "gpt-5"
        and argv[argv.index("--resume") + 1] == "chat-123"
        # Prompt is the single positional after `--`: flag-shaped prompt text
        # can never be parsed as an option.
        and argv[sep + 1:] == ["--resume sneaky prompt"]
        and argv.index("--resume") < sep
    )


def test_build_argv_default_permission_has_no_force() -> bool:
    argv = runner_cursor.build_argv(
        cursor_bin="cursor-agent", prompt="hi", model="", session_id="",
        permission={"mode": "default"},
    )
    return "-f" not in argv and "--force" not in argv and "--model" not in argv and "--resume" not in argv


def _mk_normalizer() -> "runner_cursor.CursorStreamNormalizer":
    n = runner_cursor.CursorStreamNormalizer()
    n.handle({
        "type": "system", "subtype": "init", "apiKeySource": "login",
        "cwd": "/tmp", "session_id": "chat-1", "model": "gpt-5",
        "permissionMode": "default",
    })
    return n


def test_normalizer_init_captures_session_and_model() -> bool:
    n = _mk_normalizer()
    return n.session_id == "chat-1" and n.model == "gpt-5"


def test_normalizer_skips_user_echo() -> bool:
    n = _mk_normalizer()
    out = n.handle({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        "session_id": "chat-1",
    })
    return out == []


def test_normalizer_accumulates_assistant_deltas() -> bool:
    n = _mk_normalizer()
    a = n.handle({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hel"}]},
        "session_id": "chat-1", "timestamp_ms": 1750000000000,
    })
    b = n.handle({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "lo"}]},
        "session_id": "chat-1",
    })
    if len(a) != 1 or len(b) != 1:
        return False
    ev_a, ev_b = a[0], b[0]
    return (
        ev_a["type"] == "assistant"
        and ev_a["uuid"] == ev_b["uuid"]  # same segment → stable uuid
        and ev_a["message"]["content"][0]["text"] == "Hel"
        and ev_b["message"]["content"][0]["text"] == "Hello"
        and ev_b["message"]["model"] == "gpt-5"
        and n.assistant_seen
    )


def test_normalizer_thinking_deltas() -> bool:
    n = _mk_normalizer()
    a = n.handle({"type": "thinking", "subtype": "delta", "text": "think", "session_id": "chat-1"})
    n.handle({"type": "thinking", "subtype": "completed", "session_id": "chat-1"})
    c = n.handle({"type": "thinking", "subtype": "delta", "text": "again", "session_id": "chat-1"})
    return (
        len(a) == 1 and len(c) == 1
        and a[0]["message"]["content"][0] == {"type": "thinking", "thinking": "think"}
        and c[0]["message"]["content"][0]["thinking"] == "again"
        and a[0]["uuid"] != c[0]["uuid"]  # completed closed the segment
    )


def test_normalizer_tool_call_started_maps_shell() -> bool:
    n = _mk_normalizer()
    out = n.handle({
        "type": "tool_call", "subtype": "started", "call_id": "call-1",
        "tool_call": {"shellToolCall": {"args": {
            "command": "ls -la", "workingDirectory": "/tmp",
        }}},
        "session_id": "chat-1",
    })
    if len(out) != 1:
        return False
    block = out[0]["message"]["content"][0]
    return (
        out[0]["type"] == "assistant"
        and block["type"] == "tool_use"
        and block["id"] == "call-1"
        and block["name"] == "Bash"
        and block["input"] == {"command": "ls -la", "cwd": "/tmp"}
    )


def test_normalizer_tool_call_completed_success_and_failure() -> bool:
    n = _mk_normalizer()
    ok = n.handle({
        "type": "tool_call", "subtype": "completed", "call_id": "call-1",
        "tool_call": {"shellToolCall": {
            "args": {"command": "ls"},
            "result": {"success": {"stdout": "file.py"}},
        }},
        "session_id": "chat-1",
    })
    bad = n.handle({
        "type": "tool_call", "subtype": "completed", "call_id": "call-2",
        "tool_call": {"shellToolCall": {
            "args": {"command": "boom"},
            "result": {"failure": {"stderr": "exploded", "message": "exploded"}},
        }},
        "session_id": "chat-1",
    })
    if len(ok) != 1 or len(bad) != 1:
        return False
    ok_block = ok[0]["message"]["content"][0]
    bad_block = bad[0]["message"]["content"][0]
    return (
        ok[0]["type"] == "user"
        and ok_block["type"] == "tool_result"
        and ok_block["tool_use_id"] == "call-1"
        and ok_block["content"] == "file.py"
        and ok_block["is_error"] is False
        and bad_block["is_error"] is True
        and "exploded" in bad_block["content"]
    )


def test_normalizer_tool_uuids_deterministic() -> bool:
    def run_once() -> tuple[str, str]:
        n = _mk_normalizer()
        started = n.handle({
            "type": "tool_call", "subtype": "started", "call_id": "call-9",
            "tool_call": {"readToolCall": {"args": {"path": "/tmp/x"}}},
        })
        completed = n.handle({
            "type": "tool_call", "subtype": "completed", "call_id": "call-9",
            "tool_call": {"readToolCall": {"args": {"path": "/tmp/x"},
                                           "result": {"success": {"content": "x"}}}},
        })
        return started[0]["uuid"], completed[0]["uuid"]

    a, b = run_once(), run_once()
    return a == b and a[0] != a[1]


def test_normalizer_read_tool_input_mapping() -> bool:
    n = _mk_normalizer()
    out = n.handle({
        "type": "tool_call", "subtype": "started", "call_id": "c",
        "tool_call": {"readToolCall": {"args": {"path": "/tmp/f.py"}}},
    })
    block = out[0]["message"]["content"][0]
    return block["name"] == "Read" and block["input"] == {"file_path": "/tmp/f.py"}


def test_normalizer_result_bookkeeping() -> bool:
    n = _mk_normalizer()
    out = n.handle({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 1234, "result": "pong", "session_id": "chat-1",
    })
    return (
        out == []
        and n.result_seen and n.success and not n.is_error
        and n.result_text == "pong" and n.duration_ms == 1234
    )


def test_normalizer_result_error() -> bool:
    n = _mk_normalizer()
    n.handle({"type": "result", "subtype": "error", "is_error": True,
              "result": "boom", "session_id": "chat-1"})
    return n.result_seen and not n.success and n.error == "boom"


def test_normalizer_unknown_event_surfaced() -> bool:
    n = _mk_normalizer()
    out = n.handle({"type": "totally_new_thing", "payload": 1})
    return (
        len(out) == 1
        and out[0]["type"] == "unknown_event"
        and out[0]["raw_type"] == "totally_new_thing"
        and out[0]["raw"] == {"type": "totally_new_thing", "payload": 1}
    )


def test_auth_failure_detection() -> bool:
    import runner_errors
    hit = runner_errors.classify(
        "cursor",
        "",
        "Error: Authentication required. Please run 'cursor-agent login' first",
    )
    return (
        hit is not None
        and hit.category == runner_errors.CATEGORY_AUTH
        and "cursor-agent login" in hit.message
        and runner_errors.classify("cursor", "all good", "") is None
    )


def test_runner_fails_closed_on_empty_prompt() -> bool:
    run_dir = Path(tempfile.mkdtemp(prefix="cursor-runner-test-"))
    try:
        rc = asyncio.run(runner_cursor._run(run_dir, {
            "prompt": "", "cwd": str(run_dir), "mode": "native",
            "app_session_id": "app-1",
        }))
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        return rc == 1 and complete["success"] is False and "prompt" in complete["error"]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_capability_context_labels_team_message() -> bool:
    prompt = runner_cursor._prepend_capability_context("<mssg>done</mssg>", {
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


TESTS = [
    ("class_identity", test_class_identity),
    ("capability_matrix", test_capability_matrix),
    ("build_env_clears_claude_keeps_cursor_key", test_build_env_clears_claude_keeps_cursor_key),
    ("models_static_seed", test_models_static_seed),
    ("parses_models_output", test_parses_models_output),
    ("models_output_parse_fails_closed", test_models_output_parse_fails_closed),
    ("fetch_models_returns_list", test_fetch_models_returns_list),
    ("permission_argv_fail_closed", test_permission_argv_fail_closed),
    ("resolve_permission_fail_closed", test_resolve_permission_fail_closed),
    ("build_argv_shape", test_build_argv_shape),
    ("build_argv_default_permission_has_no_force", test_build_argv_default_permission_has_no_force),
    ("normalizer_init_captures_session_and_model", test_normalizer_init_captures_session_and_model),
    ("normalizer_skips_user_echo", test_normalizer_skips_user_echo),
    ("normalizer_accumulates_assistant_deltas", test_normalizer_accumulates_assistant_deltas),
    ("normalizer_thinking_deltas", test_normalizer_thinking_deltas),
    ("normalizer_tool_call_started_maps_shell", test_normalizer_tool_call_started_maps_shell),
    ("normalizer_tool_call_completed_success_and_failure", test_normalizer_tool_call_completed_success_and_failure),
    ("normalizer_tool_uuids_deterministic", test_normalizer_tool_uuids_deterministic),
    ("normalizer_read_tool_input_mapping", test_normalizer_read_tool_input_mapping),
    ("normalizer_result_bookkeeping", test_normalizer_result_bookkeeping),
    ("normalizer_result_error", test_normalizer_result_error),
    ("normalizer_unknown_event_surfaced", test_normalizer_unknown_event_surfaced),
    ("auth_failure_detection", test_auth_failure_detection),
    ("runner_fails_closed_on_empty_prompt", test_runner_fails_closed_on_empty_prompt),
    ("capability_context_labels_team_message", test_capability_context_labels_team_message),
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
