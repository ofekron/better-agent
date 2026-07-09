"""Focused tests for the Qwen Code provider.

Pins:
  1. Capability matrix: native-only (no fork / team / steering / reasoning),
     simulated rewind on, KIND="qwen".
  2. Env: build_env clears foreign-provider vars; api_key mode routes the
     record's api_key/base_url through OPENAI_API_KEY / OPENAI_BASE_URL
     (qwen `--auth-type openai`); subscription mode sets neither.
  3. Runner normalization: qwen's Claude-shaped stream-json messages map to
     the Claude jsonl shape recovery_family="gemini" replay expects —
     assistant text/thinking passthrough, tool_use routed through
     runner_gemini's shared _map_tool (run_shell_command→Bash etc.),
     tool_result passthrough, system/result handled out-of-band, unknown
     types surfaced as diagnostics (never dropped).
  4. Approval-mode mapping: BA's gemini-style "auto_edit" → qwen's
     hyphenated "--approval-mode auto-edit"; unknown → yolo.
  5. Auth-type routing: subscription → qwen-oauth, api_key → openai.
  6. Result handling: usage_from_result token mapping + terminal-error
     extraction from is_error results.
  7. Models: static seed sanity + fetch_qwen_models parses the real
     installed CLI bundle (skipped when qwen is not installed).

NOTE: registry resolution (`_resolve_class("qwen")`) and the setup
installer are NOT tested here — they require the provider_manifest /
provider_setup entries that land with the registration wiring.

Run:
    cd backend && .venv/bin/python scripts/test_provider_qwen.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-qwen-")

import provider_qwen  # noqa: E402
import runner_qwen  # noqa: E402


def _mk(mode: str = "subscription", **extra) -> provider_qwen.QwenProvider:
    return provider_qwen.QwenProvider({"id": "q1", "kind": "qwen", "mode": mode, **extra})


def test_kind_and_capability_matrix() -> bool:
    cls = provider_qwen.QwenProvider
    expected = {
        "supports_fork": False,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": False,
    }
    return cls.KIND == "qwen" and all(getattr(cls, k) is v for k, v in expected.items())


def test_build_env_clears_foreign_providers() -> bool:
    import os
    poisoned = {
        "ANTHROPIC_API_KEY": "x", "CLAUDE_CONFIG_DIR": "x", "GEMINI_API_KEY": "x",
        "GEMINI_CLI_HOME": "x", "CODEX_HOME": "x", "OPENAI_API_KEY": "x",
        "OPENAI_BASE_URL": "x", "OPENAI_MODEL": "x",
    }
    saved = {k: os.environ.get(k) for k in poisoned}
    os.environ.update(poisoned)
    try:
        env = _mk().build_env()
        return not any(k in env for k in poisoned)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_build_env_api_key_mode_sets_openai_vars() -> bool:
    env = _mk(
        mode="api_key",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).build_env()
    sub_env = _mk().build_env()
    return (
        env.get("OPENAI_API_KEY") == "sk-test"
        and env.get("OPENAI_BASE_URL") == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        and "OPENAI_API_KEY" not in sub_env
    )


def test_approval_mode_mapping() -> bool:
    cases = [
        ({"mode": "auto_edit"}, "auto-edit"),
        ({"mode": "auto-edit"}, "auto-edit"),
        ({"mode": "plan"}, "plan"),
        ({"mode": "yolo"}, "yolo"),
        ({"mode": "bypassPermissions"}, "yolo"),
        ({}, "yolo"),
        (None, "yolo"),
        ("garbage", "yolo"),
    ]
    return all(runner_qwen.resolve_approval_mode(p) == want for p, want in cases)


def test_auth_type_routing() -> bool:
    return (
        runner_qwen.resolve_auth_type("subscription") == "qwen-oauth"
        and runner_qwen.resolve_auth_type("api_key") == "openai"
        and runner_qwen.resolve_auth_type("") == "qwen-oauth"
    )


def test_normalize_assistant_text_passthrough() -> bool:
    raw = {
        "type": "assistant",
        "uuid": "u-1",
        "session_id": "s-1",
        "parent_tool_use_id": None,
        "message": {
            "id": "u-1", "type": "message", "role": "assistant",
            "model": "coder-model",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": None,
        },
    }
    out = runner_qwen.normalize_qwen_event(raw, "parent-1", "coder-model")
    return (
        out is not None
        and out["type"] == "assistant"
        and out["uuid"] == "u-1"
        and out["parentUuid"] == "parent-1"
        and out["message"]["role"] == "assistant"
        and out["message"]["model"] == "coder-model"
        and out["message"]["content"] == [{"type": "text", "text": "OK"}]
        and isinstance(out.get("timestamp"), str)
    )


def test_normalize_assistant_thinking_passthrough() -> bool:
    raw = {
        "type": "assistant", "uuid": "u-2",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "hmm", "signature": "Plan"}],
        },
    }
    out = runner_qwen.normalize_qwen_event(raw, "p", "coder-model")
    block = out["message"]["content"][0]
    # Model falls back to the init-resolved model when absent per-message.
    return (
        block["type"] == "thinking" and block["thinking"] == "hmm"
        and out["message"]["model"] == "coder-model"
    )


def test_normalize_tool_use_maps_gemini_tool_names() -> bool:
    raw = {
        "type": "assistant", "uuid": "u-3",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": "call-1",
                "name": "run_shell_command",
                "input": {"command": "ls", "description": "list"},
            }],
        },
    }
    out = runner_qwen.normalize_qwen_event(raw, "p", "m")
    block = out["message"]["content"][0]
    if not (block["type"] == "tool_use" and block["name"] == "Bash" and block["id"] == "call-1"):
        return False
    if block["input"].get("command") != "ls":
        return False
    # read_file → Read with path → file_path key translation.
    raw2 = {
        "type": "assistant", "uuid": "u-4",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-2", "name": "read_file",
            "input": {"path": "/tmp/x.py"},
        }]},
    }
    block2 = runner_qwen.normalize_qwen_event(raw2, "p", "m")["message"]["content"][0]
    return block2["name"] == "Read" and block2["input"].get("file_path") == "/tmp/x.py"


def test_normalize_tool_result_passthrough() -> bool:
    raw = {
        "type": "user", "uuid": "u-5",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "call-1",
                "content": "file.py", "is_error": False,
            }],
        },
    }
    out = runner_qwen.normalize_qwen_event(raw, "p", "m")
    block = out["message"]["content"][0]
    return (
        out["type"] == "user"
        and block["type"] == "tool_result"
        and block["tool_use_id"] == "call-1"
        and block["content"] == "file.py"
        and "is_error" not in block
        and "model" not in out["message"]
    )


def test_normalize_tool_result_error_flag() -> bool:
    raw = {
        "type": "user", "uuid": "u-6",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "c", "content": "boom", "is_error": True,
        }]},
    }
    block = runner_qwen.normalize_qwen_event(raw, "p", "m")["message"]["content"][0]
    return block.get("is_error") is True and block["content"] == "boom"


def test_normalize_system_and_result_return_none() -> bool:
    system = {"type": "system", "subtype": "init", "session_id": "s", "model": "coder-model"}
    result = {"type": "result", "subtype": "success", "is_error": False}
    return (
        runner_qwen.normalize_qwen_event(system, "p", "m") is None
        and runner_qwen.normalize_qwen_event(result, "p", "m") is None
    )


def test_normalize_unknown_type_surfaces_diagnostic() -> bool:
    out = runner_qwen.normalize_qwen_event({"type": "mystery", "x": 1}, "p", "m")
    return (
        out is not None
        and out["type"] == "unknown_event"
        and out["raw_type"] == "mystery"
        and out["parentUuid"] == "p"
    )


def test_normalize_uuid_is_stable_for_same_event() -> bool:
    raw = {"type": "assistant", "uuid": "stable-1",
           "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}
    a = runner_qwen.normalize_qwen_event(raw, "p", "m")
    b = runner_qwen.normalize_qwen_event(raw, "p", "m")
    return a["uuid"] == b["uuid"] == "stable-1"


def test_usage_from_result_mapping() -> bool:
    raw = {
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 1234,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
    }
    usage = runner_qwen.usage_from_result(raw)
    return usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 3,
        "total_tokens": 15,
        "duration_ms": 1234,
    }


def test_terminal_error_extraction() -> bool:
    # Real shape captured from a live `qwen -o stream-json` run.
    err_result = {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "error": {"message": "No auth type is selected."},
    }
    ok_result = {"type": "result", "subtype": "success", "is_error": False}
    return (
        runner_qwen._qwen_terminal_error(err_result) == "No auth type is selected."
        and runner_qwen._qwen_terminal_error(ok_result) is None
    )


def test_models_static_seed() -> bool:
    return (
        "coder-model" in provider_qwen.QWEN_MODELS
        and "qwen3-coder-plus" in provider_qwen.QWEN_MODELS
        and len(provider_qwen.QWEN_MODELS) >= 3
    )


def test_models_fetch_parses_real_cli() -> bool:
    # Only assert when the qwen CLI is installed; the static seed covers
    # cold start otherwise.
    if not shutil.which("qwen"):
        return True
    parsed = provider_qwen.fetch_qwen_models()
    return (
        bool(parsed)
        and "coder-model" in parsed
        and "qwen3-coder-plus" in parsed
        and len(parsed) >= 3
        and not any(m.endswith((".", "-")) for m in parsed)
    )


def test_rate_limit_keywords_extended() -> bool:
    kws = provider_qwen.QwenProvider._GEMINI_RATE_LIMIT_KEYWORDS
    return "insufficient_quota" in kws and "rate limit" in kws


TESTS = [
    ("kind_and_capability_matrix", test_kind_and_capability_matrix),
    ("build_env_clears_foreign_providers", test_build_env_clears_foreign_providers),
    ("build_env_api_key_mode_sets_openai_vars", test_build_env_api_key_mode_sets_openai_vars),
    ("approval_mode_mapping", test_approval_mode_mapping),
    ("auth_type_routing", test_auth_type_routing),
    ("normalize_assistant_text_passthrough", test_normalize_assistant_text_passthrough),
    ("normalize_assistant_thinking_passthrough", test_normalize_assistant_thinking_passthrough),
    ("normalize_tool_use_maps_gemini_tool_names", test_normalize_tool_use_maps_gemini_tool_names),
    ("normalize_tool_result_passthrough", test_normalize_tool_result_passthrough),
    ("normalize_tool_result_error_flag", test_normalize_tool_result_error_flag),
    ("normalize_system_and_result_return_none", test_normalize_system_and_result_return_none),
    ("normalize_unknown_type_surfaces_diagnostic", test_normalize_unknown_type_surfaces_diagnostic),
    ("normalize_uuid_is_stable_for_same_event", test_normalize_uuid_is_stable_for_same_event),
    ("usage_from_result_mapping", test_usage_from_result_mapping),
    ("terminal_error_extraction", test_terminal_error_extraction),
    ("models_static_seed", test_models_static_seed),
    ("models_fetch_parses_real_cli", test_models_fetch_parses_real_cli),
    ("rate_limit_keywords_extended", test_rate_limit_keywords_extended),
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
