"""Focused tests for the Amp provider.

Pins:
  1. Class identity: AmpProvider subclasses SessionEventsProvider, KIND="amp".
  2. Capability matrix: fork ON (amp threads fork is real), simulated
     rewind on, no team / steering / reasoning effort.
  3. build_env: clears Claude env; routes record api_key/base_url into
     AMP_API_KEY / AMP_URL.
  4. Models: static selector catalog (agent modes + sonnet toggle) and
     the runner's flag mapping; unknown selectors fail closed.
  5. Permission: `--dangerously-allow-all` only on explicit full
     permission; empty/default permission runs without it (fail closed).
  6. Runner event normalization: Amp's Claude-Code-compatible stream-json
     (verified against a real `amp -x --stream-json` run) maps to
     Claude-shaped agent_message payloads — assistant text/tool_use pass
     through, user tool_result carriers are emitted, the prompt echo and
     system/result bookkeeping are skipped, unknown types surface as
     diagnostics — and uuids are deterministic per (thread, ordinal).
  7. argv shapes for fresh / resume turns and fork-output thread-id parse.

Run:
    cd backend && .venv/bin/python scripts/test_provider_amp.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-amp-")

import provider_amp  # noqa: E402
import runner_amp  # noqa: E402
from provider_session_events import SessionEventsProvider  # noqa: E402


# Real lines captured from `amp -x "reply with just OK" --stream-json`
# (amp 0.0.1765051277, 2026-07-09).
_REAL_SID = "T-649f00f7-106f-417a-8c4c-47aa558ab87e"
_REAL_INIT = {
    "type": "system", "subtype": "init", "cwd": "/private/tmp",
    "session_id": _REAL_SID,
    "tools": ["Bash", "Read", "edit_file"],
    "mcp_servers": [{"name": "strategy-helper", "status": "connecting"}],
}
_REAL_USER_ECHO = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "reply with just OK"}]},
    "parent_tool_use_id": None, "session_id": _REAL_SID,
}
_REAL_RESULT_ERROR = {
    "type": "result", "subtype": "error_during_execution", "duration_ms": 854,
    "is_error": True, "num_turns": 0,
    "error": '402 {"type":"error","error":{"type":"unknown_error","message":"Execute mode..."}}',
    "session_id": _REAL_SID,
}


def test_class_identity():
    cls = provider_amp.AmpProvider
    assert issubclass(cls, SessionEventsProvider)
    assert cls.KIND == "amp"


def test_capability_matrix():
    cls = provider_amp.AmpProvider
    expected = {
        "supports_fork": True,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": False,
    }
    for key, value in expected.items():
        assert getattr(cls, key) is value


def test_build_env_clears_claude_and_routes_credentials():
    inst = provider_amp.AmpProvider({
        "id": "a1", "kind": "amp", "mode": "api_key",
        "api_key": "test-amp-key", "base_url": "https://amp.example.com",
        "_credential_authoritative": True,
        "_credential_status": "available",
    })
    env = inst.build_env()
    for foreign in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    ):
        assert foreign not in env
    assert env.get("AMP_API_KEY") == "test-amp-key"
    assert env.get("AMP_URL") == "https://amp.example.com"


def test_build_env_subscription_no_injection():
    inst = provider_amp.AmpProvider({"id": "a2", "kind": "amp", "mode": "subscription"})
    env = inst.build_env()
    inherited = __import__("os").environ.get("AMP_API_KEY")
    # Without a record api_key the env only carries whatever the process
    # inherited — the provider must not invent a key.
    assert env.get("AMP_API_KEY") == inherited


def test_models_static_catalog():
    models = provider_amp.AMP_MODELS
    assert models[0] == "auto"
    assert set(models) == {"auto", "smart", "rush", "free", "sonnet"}
    assert provider_amp.fetch_amp_models() == models


def test_model_argv_mapping():
    cases = {
        "": [],
        "auto": [],
        "smart": ["-m", "smart"],
        "rush": ["-m", "rush"],
        "free": ["-m", "free"],
        "sonnet": ["--use-sonnet"],
    }
    for selector, expected in cases.items():
        assert runner_amp.model_argv(selector) == expected
    # Unknown selectors must fail closed.
    try:
        runner_amp.model_argv("gpt-5")
    except ValueError:
        pass
    else:
        raise AssertionError("model_argv should reject unknown selectors")


def test_permission_argv_fail_closed():
    assert runner_amp.permission_argv({}) == []
    assert runner_amp.permission_argv(None) == []
    assert runner_amp.permission_argv({"mode": "default"}) == []
    assert runner_amp.permission_argv({"mode": "dangerously-allow-all"}) == ["--dangerously-allow-all"]
    assert runner_amp.permission_argv({"mode": "bypassPermissions"}) == ["--dangerously-allow-all"]


def test_build_argv_fresh_and_resume():
    fresh = runner_amp.build_amp_argv(
        "/bin/amp", resume_thread_id=None, model="smart",
        permission={"mode": "dangerously-allow-all"},
    )
    resume = runner_amp.build_amp_argv(
        "/bin/amp", resume_thread_id=_REAL_SID, model="", permission={},
    )
    assert fresh == ["/bin/amp", "-m", "smart", "--dangerously-allow-all", "-x", "--stream-json"]
    assert resume == ["/bin/amp", "threads", "continue", _REAL_SID, "-x", "--stream-json"]


def test_parse_fork_thread_id():
    text = f"Created new thread {_REAL_SID} (forked)\n"
    assert runner_amp.parse_fork_thread_id(text) == _REAL_SID
    assert runner_amp.parse_fork_thread_id("no id here") is None


def _normalize(event, index=0, sid=_REAL_SID):
    return runner_amp.normalize_amp_event(
        event, session_id=sid, parent_uuid=sid, model="auto", event_index=index,
    )


def test_normalizer_skips_bookkeeping_and_prompt_echo():
    assert _normalize(_REAL_INIT) is None
    assert _normalize(_REAL_RESULT_ERROR) is None
    assert _normalize(_REAL_USER_ECHO) is None


def test_normalizer_assistant_text_and_tool_use():
    text_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
        },
        "session_id": _REAL_SID, "parent_tool_use_id": None,
    }
    tool_event = {
        "type": "assistant",
        "message": {
            "role": "assistant", "model": "claude-opus-4.5",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash",
                         "input": {"cmd": "ls"}}],
        },
        "session_id": _REAL_SID,
    }
    out_text = _normalize(text_event, index=0)
    out_tool = _normalize(tool_event, index=1)
    assert out_text is not None
    assert out_tool is not None
    assert out_text["type"] == "assistant"
    assert out_text["parentUuid"] == _REAL_SID
    assert out_text["message"]["content"][0]["text"] == "OK"
    # Missing model falls back to the run selector, never a blank.
    assert out_text["message"]["model"] == "auto"
    # Provider-reported model passes through untouched.
    assert out_tool["message"]["model"] == "claude-opus-4.5"
    assert out_tool["message"]["content"][0]["type"] == "tool_use"
    assert out_tool["message"]["content"][0]["id"] == "tu_1"


def test_normalizer_user_tool_result_emitted():
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1",
                         "content": "file.py"}],
        },
        "session_id": _REAL_SID, "parent_tool_use_id": None,
    }
    out = _normalize(event, index=2)
    assert out is not None
    assert out["type"] == "user"
    assert out["message"]["content"][0]["type"] == "tool_result"
    assert out["message"]["content"][0]["tool_use_id"] == "tu_1"


def test_normalizer_unknown_type_surfaces_diagnostic():
    out = _normalize({"type": "mystery_event", "payload": 1}, index=3)
    assert out is not None
    assert out["type"] == "unknown_event"
    assert out["raw_type"] == "mystery_event"
    assert out["raw"] == {"type": "mystery_event", "payload": 1}


def test_uuid_deterministic_per_thread_ordinal():
    event = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    }
    a = _normalize(event, index=5)
    b = _normalize(event, index=5)
    c = _normalize(event, index=6)
    d = _normalize(event, index=5, sid="T-other")
    assert a["uuid"] == b["uuid"]
    assert a["uuid"] != c["uuid"]
    assert a["uuid"] != d["uuid"]


def test_result_error_extraction():
    ok = runner_amp.result_error({"type": "result", "subtype": "success",
                                  "is_error": False, "result": "OK"})
    err = runner_amp.result_error(_REAL_RESULT_ERROR)
    assert ok is None
    assert err is not None
    assert err.startswith("402")


def test_result_token_usage():
    usage = runner_amp.result_token_usage({
        "type": "result", "subtype": "success", "is_error": False,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "total_cost_usd": 0.01, "duration_ms": 854,
    })
    empty = runner_amp.result_token_usage({"type": "result", "subtype": "success"})
    assert usage == {"input_tokens": 10, "output_tokens": 2,
                     "total_cost_usd": 0.01, "duration_ms": 854}
    assert empty is None


def test_auth_failure_detection():
    import runner_errors
    hit = runner_errors.classify(
        "amp",
        "",
        "Error: API key is not configured. Run `amp login` or set AMP_API_KEY.",
    )
    miss = runner_errors.classify("amp", "all good", "")
    assert hit is not None
    assert hit.category == runner_errors.CATEGORY_AUTH
    assert "amp login" in hit.message
    assert miss is None


def test_capability_context_labels_team_message():
    from capability_contexts import prompt_heading_for_source

    assert prompt_heading_for_source("mssg") == "Message"
    assert prompt_heading_for_source("team_ask") == "Ask"


TESTS = [
    ("class_identity", test_class_identity),
    ("capability_matrix", test_capability_matrix),
    ("build_env_clears_claude_and_routes_credentials", test_build_env_clears_claude_and_routes_credentials),
    ("build_env_subscription_no_injection", test_build_env_subscription_no_injection),
    ("models_static_catalog", test_models_static_catalog),
    ("model_argv_mapping", test_model_argv_mapping),
    ("permission_argv_fail_closed", test_permission_argv_fail_closed),
    ("build_argv_fresh_and_resume", test_build_argv_fresh_and_resume),
    ("parse_fork_thread_id", test_parse_fork_thread_id),
    ("normalizer_skips_bookkeeping_and_prompt_echo", test_normalizer_skips_bookkeeping_and_prompt_echo),
    ("normalizer_assistant_text_and_tool_use", test_normalizer_assistant_text_and_tool_use),
    ("normalizer_user_tool_result_emitted", test_normalizer_user_tool_result_emitted),
    ("normalizer_unknown_type_surfaces_diagnostic", test_normalizer_unknown_type_surfaces_diagnostic),
    ("uuid_deterministic_per_thread_ordinal", test_uuid_deterministic_per_thread_ordinal),
    ("result_error_extraction", test_result_error_extraction),
    ("result_token_usage", test_result_token_usage),
    ("auth_failure_detection", test_auth_failure_detection),
    ("capability_context_labels_team_message", test_capability_context_labels_team_message),
]


def main():
    failures = []
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError:
                failures.append(name)
                print(f"FAIL: {name}")
            except Exception:
                import traceback
                traceback.print_exc()
                failures.append(name)
                print(f"FAIL: {name} (unexpected exception)")
            else:
                print(f"PASS: {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
