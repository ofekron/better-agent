"""Focused tests for the Qwen Code provider.

Pins:
  1. Capability matrix: native-only (no fork / team / steering / reasoning),
     simulated rewind on, KIND="qwen".
  2. Env: build_env clears foreign-provider vars; api_key mode routes the
     record's api_key/base_url through OPENAI_API_KEY / OPENAI_BASE_URL
     (qwen `--auth-type openai`); subscription mode sets neither.
  3. Runner normalization: qwen's Claude-shaped stream-json messages map to
     the Claude jsonl shape recovery_family="session_events" replay expects —
     assistant text/thinking passthrough, tool_use routed through
     runner_session_events' shared _map_tool (run_shell_command→Bash etc.),
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
_TMP_HOME = _test_home.isolate("bc-test-provider-qwen-")

import provider_qwen  # noqa: E402
import runner_qwen  # noqa: E402


def _mk(mode: str = "subscription", **extra) -> provider_qwen.QwenProvider:
    return provider_qwen.QwenProvider({"id": "q1", "kind": "qwen", "mode": mode, **extra})


def test_kind_and_capability_matrix() -> None:
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
    assert cls.KIND == "qwen"
    assert all(getattr(cls, k) is v for k, v in expected.items())


def test_build_env_clears_foreign_providers() -> None:
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
        assert not any(k in env for k in poisoned)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_build_env_api_key_mode_sets_openai_vars() -> None:
    env = _mk(
        mode="api_key",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        _credential_authoritative=True,
        _credential_status="available",
    ).build_env()
    sub_env = _mk().build_env()
    assert env.get("OPENAI_API_KEY") == "sk-test"
    assert env.get("OPENAI_BASE_URL") == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert "OPENAI_API_KEY" not in sub_env


def test_approval_mode_mapping() -> None:
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
    assert all(runner_qwen.resolve_approval_mode(p) == want for p, want in cases)


def test_auth_type_routing() -> None:
    assert runner_qwen.resolve_auth_type("subscription") == "qwen-oauth"
    assert runner_qwen.resolve_auth_type("api_key") == "openai"
    assert runner_qwen.resolve_auth_type("") == "qwen-oauth"


def test_normalize_assistant_text_passthrough() -> None:
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
    assert out is not None
    assert out["type"] == "assistant"
    assert out["uuid"] == "u-1"
    assert out["parentUuid"] == "parent-1"
    assert out["message"]["role"] == "assistant"
    assert out["message"]["model"] == "coder-model"
    assert out["message"]["content"] == [{"type": "text", "text": "OK"}]
    assert isinstance(out.get("timestamp"), str)


def test_normalize_assistant_thinking_passthrough() -> None:
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
    assert block["type"] == "thinking"
    assert block["thinking"] == "hmm"
    assert out["message"]["model"] == "coder-model"


def test_normalize_tool_use_maps_gemini_tool_names() -> None:
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
    assert block["type"] == "tool_use"
    assert block["name"] == "Bash"
    assert block["id"] == "call-1"
    assert block["input"].get("command") == "ls"
    # read_file → Read with path → file_path key translation.
    raw2 = {
        "type": "assistant", "uuid": "u-4",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-2", "name": "read_file",
            "input": {"path": "/tmp/x.py"},
        }]},
    }
    block2 = runner_qwen.normalize_qwen_event(raw2, "p", "m")["message"]["content"][0]
    assert block2["name"] == "Read"
    assert block2["input"].get("file_path") == "/tmp/x.py"


def test_normalize_tool_result_passthrough() -> None:
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
    assert out["type"] == "user"
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call-1"
    assert block["content"] == "file.py"
    assert "is_error" not in block
    assert "model" not in out["message"]


def test_normalize_tool_result_error_flag() -> None:
    raw = {
        "type": "user", "uuid": "u-6",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "c", "content": "boom", "is_error": True,
        }]},
    }
    block = runner_qwen.normalize_qwen_event(raw, "p", "m")["message"]["content"][0]
    assert block.get("is_error") is True
    assert block["content"] == "boom"


def test_normalize_system_and_result_return_none() -> None:
    system = {"type": "system", "subtype": "init", "session_id": "s", "model": "coder-model"}
    result = {"type": "result", "subtype": "success", "is_error": False}
    assert runner_qwen.normalize_qwen_event(system, "p", "m") is None
    assert runner_qwen.normalize_qwen_event(result, "p", "m") is None


def test_normalize_unknown_type_surfaces_diagnostic() -> None:
    out = runner_qwen.normalize_qwen_event({"type": "mystery", "x": 1}, "p", "m")
    assert out is not None
    assert out["type"] == "unknown_event"
    assert out["raw_type"] == "mystery"
    assert out["parentUuid"] == "p"


def test_normalize_uuid_is_stable_for_same_event() -> None:
    raw = {"type": "assistant", "uuid": "stable-1",
           "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}
    a = runner_qwen.normalize_qwen_event(raw, "p", "m")
    b = runner_qwen.normalize_qwen_event(raw, "p", "m")
    assert a["uuid"] == b["uuid"] == "stable-1"


def test_usage_from_result_mapping() -> None:
    raw = {
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 1234,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
    }
    usage = runner_qwen.usage_from_result(raw)
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 3,
        "total_tokens": 15,
        "duration_ms": 1234,
    }


def test_terminal_error_extraction() -> None:
    # Real shape captured from a live `qwen -o stream-json` run.
    err_result = {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "error": {"message": "No auth type is selected."},
    }
    ok_result = {"type": "result", "subtype": "success", "is_error": False}
    assert runner_qwen._qwen_terminal_error(err_result) == "No auth type is selected."
    assert runner_qwen._qwen_terminal_error(ok_result) is None


def test_models_static_seed() -> None:
    assert "coder-model" in provider_qwen.QWEN_MODELS
    assert "qwen3-coder-plus" in provider_qwen.QWEN_MODELS
    assert len(provider_qwen.QWEN_MODELS) >= 3


def test_models_fetch_parses_real_cli() -> None:
    # Only assert when the qwen CLI is installed; the static seed covers
    # cold start otherwise.
    if not shutil.which("qwen"):
        return
    parsed = provider_qwen.fetch_qwen_models()
    assert parsed
    assert "coder-model" in parsed
    assert "qwen3-coder-plus" in parsed
    assert len(parsed) >= 3
    assert not any(m.endswith((".", "-")) for m in parsed)


def test_rate_limit_keywords_extended() -> None:
    kws = provider_qwen.QwenProvider._RATE_LIMIT_KEYWORDS
    assert "insufficient_quota" in kws
    assert "rate limit" in kws


def test_materializes_selected_extension_mcp_in_scoped_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_home = root / "source-home"
        source_qwen = source_home / ".qwen"
        source_qwen.mkdir(parents=True)
        (source_qwen / "settings.json").write_text(
            json.dumps({"theme": "dark"}),
            encoding="utf-8",
        )
        (source_qwen / "oauth_creds.json").write_text("credentials", encoding="utf-8")
        run_dir = root / "run"
        run_dir.mkdir()
        env = runner_qwen._materialize_qwen_run_home(
            run_dir,
            {
                "skills": {"implement": "instructions"},
                "mcp_servers": {
                    "testape": {
                        "command": "/fake/testape-mcp",
                        "args": [],
                    },
                },
            },
            real_home=source_home,
        )
        assert env
        overlay_home = Path(env["HOME"])
        settings = json.loads(
            (overlay_home / ".qwen" / "settings.json").read_text(encoding="utf-8")
        )
        assert settings["theme"] == "dark"
        assert settings["mcpServers"]["testape"]["command"] == "/fake/testape-mcp"
        assert (overlay_home / ".qwen" / "oauth_creds.json").read_text(encoding="utf-8") == "credentials"


def test_run_uses_frozen_mcp_resolution() -> None:
    original_runtime_env = runner_qwen.native_mcp_runtime_env
    original_effective = runner_qwen.effective_mcp_servers
    seen: dict = {}

    def fake_runtime_env(inputs: dict) -> dict:
        seen["runtime_kind"] = inputs.get("provider_kind")
        return {}

    def fake_effective(plan: dict) -> dict:
        seen["plan"] = plan
        return {"frozen": {"command": "frozen-mcp"}}

    runner_qwen.native_mcp_runtime_env = fake_runtime_env  # type: ignore[assignment]
    runner_qwen.effective_mcp_servers = fake_effective  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            code = asyncio.run(runner_qwen._run(run_dir, {
                "prompt": "check",
                "cwd": str(run_dir),
                "provider_run_config": {"skills": {"implement": "instructions"}},
                "_capability_plan": {"frozen": True},
                "_provider_executable": None,
            }))
        assert code == 1
        assert seen["runtime_kind"] == "qwen"
        assert seen["plan"] == {"frozen": True}
    finally:
        runner_qwen.native_mcp_runtime_env = original_runtime_env  # type: ignore[assignment]
        runner_qwen.effective_mcp_servers = original_effective  # type: ignore[assignment]


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
    ("materializes_selected_extension_mcp_in_scoped_home", test_materializes_selected_extension_mcp_in_scoped_home),
    ("run_uses_frozen_mcp_resolution", test_run_uses_frozen_mcp_resolution),
]


def main() -> None:
    failures = []
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError:
                print(f"FAIL: {name}")
                failures.append(name)
            except Exception:
                import traceback
                traceback.print_exc()
                print(f"FAIL: {name}")
                failures.append(name)
            else:
                print(f"PASS: {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
