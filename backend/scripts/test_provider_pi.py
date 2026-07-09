"""Focused tests for the pi coding agent provider.

Pins:
  1. Capability matrix: fork + reasoning effort (pi --thinking levels) on,
     simulated rewind on, no team/steering/native-subagents.
  2. Models: static cold-start seed shape, `pi --list-models` table parsing
     (including the logged-out "No models available" case), custom
     `provider/id` models allowed, thinking-suffix stripping.
  3. Runner event normalization: pi AgentSessionEvent messages map to the
     correct Claude-shaped events (assistant text/thinking/tool_use,
     toolResult → tool_result), tool name/input-key mapping, deterministic
     tool_result uuid, terminal stopReason → run error, usage extraction.
  4. Session continuity: `find_session_file_for_sid` locates the pi session
     jsonl across run dirs (newest wins) and returns None when absent.

Run:
    cd backend && .venv/bin/python scripts/test_provider_pi.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-pi-")

import provider_pi  # noqa: E402
import runner_pi  # noqa: E402
from runs_dir import runs_root  # noqa: E402


def test_capability_matrix() -> bool:
    cls = provider_pi.PiProvider
    expected = {
        "supports_fork": True,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": True,
        "supports_headless_no_tools": True,
    }
    return (
        cls.KIND == "pi"
        and all(getattr(cls, k) is v for k, v in expected.items())
        and cls.reasoning_effort_options == ("off", "minimal", "low", "medium", "high", "xhigh")
    )


def test_build_env_clears_claude_harness() -> bool:
    inst = provider_pi.PiProvider({"id": "p1", "kind": "pi", "mode": "api_key"})
    env = inst.build_env()
    return not any(k in env for k in (
        "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    ))


def test_models_static_seed_shape() -> bool:
    seed = provider_pi.PI_MODELS
    return (
        len(seed) >= 5
        and all("/" in m for m in seed)
        and "anthropic/claude-sonnet-4-6" in seed
        and "openai/gpt-5.5" in seed
    )


def test_parses_list_models_table() -> bool:
    sample = (
        "provider   model                 context  max-out  thinking  images\n"
        "anthropic  claude-sonnet-4-6     200K     64K      yes       yes\n"
        "anthropic  claude-haiku-4-5      200K     64K      yes       yes\n"
        "openai     gpt-5.4               400K     128K     yes       yes\n"
    )
    return provider_pi._parse_pi_list_models(sample) == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
        "openai/gpt-5.4",
    ]


def test_parses_logged_out_list_models() -> bool:
    sample = (
        "No models available. Use /login to log into a provider via OAuth "
        "or API key. See:\n  /some/path/providers.md\n"
    )
    return provider_pi._parse_pi_list_models(sample) == []


def test_model_allowed_semantics() -> bool:
    available = list(provider_pi.PI_MODELS)
    return (
        provider_pi._model_allowed("anthropic/claude-sonnet-4-6", available)
        # thinking suffix strips before validation
        and provider_pi._model_allowed("anthropic/claude-sonnet-4-6:high", available)
        # custom provider/id pairs (user models.json) are spawnable
        and provider_pi._model_allowed("ollama/qwen2.5-coder:7b", available)
        # bare non-catalog name without provider prefix is rejected
        and not provider_pi._model_allowed("sonnet", available)
    )


def _assistant_message(**overrides) -> dict:
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "usage": {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 0,
                  "totalTokens": 17},
        "stopReason": "stop",
        "timestamp": 1760000000000,
    }
    message.update(overrides)
    return message


def test_normalizes_assistant_text_and_thinking() -> bool:
    message = _assistant_message(content=[
        {"type": "thinking", "thinking": "pondering"},
        {"type": "text", "text": "hello"},
    ])
    out = runner_pi.normalize_assistant_message(
        message, parent_uuid="root", msg_uuid="u1", fallback_model="pi",
    )
    if out is None or out["type"] != "assistant" or out["uuid"] != "u1":
        return False
    if out["parentUuid"] != "root":
        return False
    blocks = out["message"]["content"]
    return (
        blocks[0] == {"type": "thinking", "thinking": "pondering"}
        and blocks[1] == {"type": "text", "text": "hello"}
        and out["message"]["model"] == "anthropic/claude-sonnet-4-6"
    )


def test_normalizes_tool_calls_with_key_mapping() -> bool:
    message = _assistant_message(content=[
        {"type": "toolCall", "id": "call_1", "name": "edit",
         "arguments": {"path": "/tmp/f.py", "oldText": "a", "newText": "b"}},
        {"type": "toolCall", "id": "call_2", "name": "bash",
         "arguments": {"command": "ls -la"}},
        {"type": "toolCall", "id": "call_3", "name": "find",
         "arguments": {"pattern": "*.py", "path": "."}},
    ])
    out = runner_pi.normalize_assistant_message(
        message, parent_uuid="root", msg_uuid="u2", fallback_model="pi",
    )
    if out is None:
        return False
    edit, bash, find = out["message"]["content"]
    return (
        edit["type"] == "tool_use" and edit["name"] == "Edit"
        and edit["input"] == {"file_path": "/tmp/f.py", "old_string": "a", "new_string": "b"}
        and bash["name"] == "Bash" and bash["input"] == {"command": "ls -la"}
        and find["name"] == "Glob" and find["input"] == {"pattern": "*.py", "path": "."}
        and edit["id"] == "call_1"
    )


def test_normalizes_tool_result() -> bool:
    message = {
        "role": "toolResult",
        "toolCallId": "call_1",
        "toolName": "read",
        "content": [{"type": "text", "text": "file contents"}],
        "isError": False,
        "timestamp": 1760000000000,
    }
    out = runner_pi.normalize_tool_result_message(
        message, parent_uuid="p", session_id="sid-1",
    )
    if out is None or out["type"] != "user":
        return False
    block = out["message"]["content"][0]
    return (
        block["type"] == "tool_result"
        and block["tool_use_id"] == "call_1"
        and block["content"] == "file contents"
        and block["is_error"] is False
    )


def test_tool_result_uuid_is_deterministic() -> bool:
    message = {
        "role": "toolResult", "toolCallId": "call_9", "toolName": "bash",
        "content": [{"type": "text", "text": "ok"}], "isError": False,
    }
    a = runner_pi.normalize_tool_result_message(message, parent_uuid="p", session_id="s")
    b = runner_pi.normalize_tool_result_message(message, parent_uuid="p", session_id="s")
    c = runner_pi.normalize_tool_result_message(message, parent_uuid="p", session_id="OTHER")
    return a["uuid"] == b["uuid"] and a["uuid"] != c["uuid"]


def test_error_stop_reason_detection() -> bool:
    ok = runner_pi.error_from_assistant_message(_assistant_message()) is None
    err = runner_pi.error_from_assistant_message(
        _assistant_message(stopReason="error", errorMessage="rate limited")
    )
    aborted = runner_pi.error_from_assistant_message(
        _assistant_message(stopReason="aborted", errorMessage=None)
    )
    return ok and err == "rate limited" and aborted == "Request aborted"


def test_usage_extraction_and_sum() -> bool:
    usage = runner_pi._usage_from_message(_assistant_message())
    if usage != {"input_tokens": 10, "output_tokens": 5,
                 "cache_read_input_tokens": 2, "total_tokens": 17}:
        return False
    summed = runner_pi._sum_usage(usage, usage)
    return summed["total_tokens"] == 34 and summed["input_tokens"] == 20


def test_empty_assistant_message_skipped() -> bool:
    out = runner_pi.normalize_assistant_message(
        _assistant_message(content=[]),
        parent_uuid="root", msg_uuid="u3", fallback_model="pi",
    )
    return out is None


def test_unknown_event_surfaces_as_diagnostic() -> bool:
    out = runner_pi.normalize_unknown_event(
        {"type": "brand_new_event", "payload": 1}, parent_uuid="p",
    )
    return (
        out["type"] == "unknown_event"
        and out["raw_type"] == "brand_new_event"
        and out["raw"] == {"type": "brand_new_event", "payload": 1}
    )


def test_auth_failure_detection() -> bool:
    msg = runner_pi._auth_failure_from_stderr(
        "No API key found for the selected model.\n\nUse /login to log into "
        "a provider via OAuth or API key."
    )
    return bool(msg) and "credentials" in msg and runner_pi._auth_failure_from_stderr("boom") is None


def test_find_session_file_for_sid() -> bool:
    sid = "019f486c-b3c1-712a-acf3-02a47e358514"
    root = runs_root()
    old = root / "run-old" / runner_pi.PI_SESSION_DIR_NAME / "--tmp--"
    new = root / "run-new" / runner_pi.PI_SESSION_DIR_NAME / "--tmp--"
    old.mkdir(parents=True, exist_ok=True)
    new.mkdir(parents=True, exist_ok=True)
    old_file = old / f"2026-07-01T00-00-00_{sid}.jsonl"
    new_file = new / f"2026-07-09T00-00-00_{sid}.jsonl"
    old_file.write_text("{}\n", encoding="utf-8")
    time.sleep(0.02)
    new_file.write_text("{}\n", encoding="utf-8")
    found = runner_pi.find_session_file_for_sid(sid)
    missing = runner_pi.find_session_file_for_sid("no-such-sid")
    return found == new_file and missing is None


def test_capability_context_labels_team_message() -> bool:
    from capability_contexts import prepend_capability_context
    prompt = prepend_capability_context("<mssg>done</mssg>", {
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


def test_models_fetch_handles_missing_or_real_cli() -> bool:
    # Never asserts on the developer's login state: parses only when the CLI
    # is installed AND authenticated; [] is a valid (logged-out) result.
    parsed = provider_pi.fetch_pi_models()
    if not parsed:
        return True
    return all("/" in m for m in parsed) and len(parsed) >= 3


TESTS = [
    ("capability_matrix", test_capability_matrix),
    ("build_env_clears_claude_harness", test_build_env_clears_claude_harness),
    ("models_static_seed_shape", test_models_static_seed_shape),
    ("parses_list_models_table", test_parses_list_models_table),
    ("parses_logged_out_list_models", test_parses_logged_out_list_models),
    ("model_allowed_semantics", test_model_allowed_semantics),
    ("normalizes_assistant_text_and_thinking", test_normalizes_assistant_text_and_thinking),
    ("normalizes_tool_calls_with_key_mapping", test_normalizes_tool_calls_with_key_mapping),
    ("normalizes_tool_result", test_normalizes_tool_result),
    ("tool_result_uuid_is_deterministic", test_tool_result_uuid_is_deterministic),
    ("error_stop_reason_detection", test_error_stop_reason_detection),
    ("usage_extraction_and_sum", test_usage_extraction_and_sum),
    ("empty_assistant_message_skipped", test_empty_assistant_message_skipped),
    ("unknown_event_surfaces_as_diagnostic", test_unknown_event_surfaces_as_diagnostic),
    ("auth_failure_detection", test_auth_failure_detection),
    ("find_session_file_for_sid", test_find_session_file_for_sid),
    ("capability_context_labels_team_message", test_capability_context_labels_team_message),
    ("models_fetch_handles_missing_or_real_cli", test_models_fetch_handles_missing_or_real_cli),
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
