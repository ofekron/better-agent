"""Focused tests for the OpenCode provider.

Pins:
  1. Provider surface: KIND="opencode", capability matrix (real fork via
     `--fork`, reasoning effort via `--variant`, no team/steering,
     simulated rewind on).
  2. Env: build_env clears Claude session env but keeps ANTHROPIC_API_KEY
     (a legitimate opencode credential source).
  3. Models: static credential-free seed + `opencode models` output
     parsing (and the real CLI parse when installed).
  4. Runner argv: stdin-only prompt (never argv), resume `-s`, fork
     `--fork` (requires sid — fail closed), `--variant`, image `-f`.
  5. Permission mapping: auto → `--auto`; default → no flag; readonly →
     OPENCODE_PERMISSION deny json; unknown mode raises (fail closed).
  6. Runner event normalization: each OpenCode `--format json` event type
     maps to the correct Claude-shaped event(s); tool completion emits a
     tool_use + tool_result pair; bookkeeping events are skipped; unknown
     types are surfaced (never dropped); streamed updates of one part
     produce the same render uuid (dedup/replace, not duplicate).

Run:
    cd backend && .venv/bin/python scripts/test_provider_opencode.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-opencode-")

import provider_opencode  # noqa: E402
import runner_opencode  # noqa: E402


_SID = "ses_0b79206b6ffepfkQFkR4mbrdcA"


def test_kind_and_capability_matrix() -> None:
    cls = provider_opencode.OpencodeProvider
    expected = {
        "supports_fork": True,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "rewind_requires_agent_identity": False,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": True,
        "supports_headless_no_tools": False,
    }
    assert cls.KIND == "opencode"
    assert all(getattr(cls, k) is v for k, v in expected.items())
    assert cls.reasoning_effort_options == ("minimal", "high", "max")


def test_build_env_scrubs_claude_session_env() -> None:
    import os
    inst = provider_opencode.OpencodeProvider(
        {"id": "oc1", "kind": "opencode", "mode": "api_key"}
    )
    os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        env = inst.build_env()
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
    assert not any(k in env for k in (
        "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    ))
    # ANTHROPIC_API_KEY is a valid opencode credential source — kept.
    assert env.get("ANTHROPIC_API_KEY") == "test-key"


def test_models_static_seed() -> None:
    seed = provider_opencode.OPENCODE_MODELS
    assert seed
    assert all("/" in m for m in seed)
    assert "opencode/big-pickle" in seed


def test_parses_models_output() -> None:
    sample = """
opencode/big-pickle
anthropic/claude-sonnet-4-5
opencode/north-mini-code-free
INFO some stray log line
anthropic/claude-sonnet-4-5
not a model line at all
"""
    assert provider_opencode.parse_opencode_models(sample) == [
        "opencode/big-pickle",
        "anthropic/claude-sonnet-4-5",
        "opencode/north-mini-code-free",
    ]


def test_models_fetch_parses_real_cli() -> None:
    # Only assert when the opencode CLI is installed on PATH; otherwise
    # skip (the static seed covers cold-start).
    if not shutil.which("opencode"):
        return
    parsed = provider_opencode.fetch_opencode_models()
    assert parsed
    assert all("/" in m for m in parsed)


def test_argv_stdin_only_no_prompt_leakage() -> None:
    argv = runner_opencode.build_opencode_argv(
        opencode_bin="/usr/bin/opencode",
        model="opencode/big-pickle",
        reasoning_effort=None,
        session_id=None,
        fork=False,
        permission_argv=[],
        cwd="/work/project",
    )
    # Prompt is piped over stdin — argv must never contain it.
    assert argv == ["/usr/bin/opencode", "run", "--format", "json",
                    "--dir", "/work/project",
                    "-m", "opencode/big-pickle"]


def test_argv_resume_fork_variant_files() -> None:
    argv = runner_opencode.build_opencode_argv(
        opencode_bin="opencode",
        model="anthropic/claude-sonnet-4-5",
        reasoning_effort="high",
        session_id=_SID,
        fork=True,
        permission_argv=["--auto"],
        attachment_paths=[Path("/tmp/att/a.png")],
    )
    assert argv == [
        "opencode", "run", "--format", "json",
        "-m", "anthropic/claude-sonnet-4-5",
        "--variant", "high",
        "-s", _SID, "--fork",
        "-f", "/tmp/att/a.png",
        "--auto",
    ]


def test_argv_fork_without_session_fails_closed() -> None:
    try:
        runner_opencode.build_opencode_argv(
            opencode_bin="opencode",
            model=None,
            reasoning_effort=None,
            session_id=None,
            fork=True,
            permission_argv=[],
        )
    except ValueError:
        return
    raise AssertionError("fork without a session must fail closed")


def test_permission_mapping() -> None:
    import json as _json
    auto_argv, auto_env = runner_opencode.resolve_permission_spawn({"mode": "auto"})
    default_argv, default_env = runner_opencode.resolve_permission_spawn({})
    ro_argv, ro_env = runner_opencode.resolve_permission_spawn({"mode": "readonly"})
    assert auto_argv == ["--auto"]
    assert not auto_env
    assert not default_argv
    assert not default_env
    assert not ro_argv
    denied = _json.loads(ro_env.get("OPENCODE_PERMISSION", "{}"))
    assert denied == {"bash": "deny", "edit": "deny", "write": "deny", "patch": "deny"}


def test_permission_unknown_mode_fails_closed() -> None:
    try:
        runner_opencode.resolve_permission_spawn({"mode": "yolo"})
    except ValueError:
        return
    raise AssertionError("unknown permission mode must fail closed")


def _norm(event: dict) -> list[dict]:
    return runner_opencode.normalize_opencode_event(
        event, session_id=_SID, parent_uuid=_SID, model="opencode/big-pickle",
    )


def test_runner_normalizes_text_event() -> None:
    out = _norm({
        "type": "text", "timestamp": 1783626598785, "sessionID": _SID,
        "part": {"id": "prt_1", "messageID": "msg_1", "sessionID": _SID,
                 "type": "text", "text": "PONG"},
    })
    assert len(out) == 1
    ev = out[0]
    block = ev["message"]["content"][0]
    assert ev["type"] == "assistant"
    assert ev["parentUuid"] == _SID
    assert ev["message"]["model"] == "opencode/big-pickle"
    assert block == {"type": "text", "text": "PONG"}


def test_runner_normalizes_reasoning_event() -> None:
    out = _norm({
        "type": "reasoning", "sessionID": _SID,
        "part": {"id": "prt_r", "type": "reasoning", "text": "thinking hard"},
    })
    assert len(out) == 1
    assert out[0]["type"] == "assistant"
    assert out[0]["message"]["content"][0] == {"type": "thinking", "thinking": "thinking hard"}


def test_runner_normalizes_completed_tool_event() -> None:
    out = _norm({
        "type": "tool_use", "timestamp": 1783626624808, "sessionID": _SID,
        "part": {
            "type": "tool", "tool": "read", "callID": "read_wx16",
            "state": {
                "status": "completed",
                "input": {"filePath": "/tmp/sample.txt"},
                "output": "hello",
            },
            "id": "prt_t1", "sessionID": _SID, "messageID": "msg_1",
        },
    })
    assert len(out) == 2
    use, result = out
    use_block = use["message"]["content"][0]
    result_block = result["message"]["content"][0]
    assert use["type"] == "assistant"
    assert use_block["type"] == "tool_use"
    assert use_block["id"] == "read_wx16"
    assert use_block["name"] == "Read"                       # name mapped
    assert use_block["input"] == {"file_path": "/tmp/sample.txt"}  # key mapped
    assert result["type"] == "user"
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "read_wx16"
    assert result_block["content"] == "hello"
    assert result_block["is_error"] is False


def test_runner_running_tool_emits_use_only_then_result_on_completion() -> None:
    running = _norm({
        "type": "tool_use", "sessionID": _SID,
        "part": {"type": "tool", "tool": "bash", "callID": "b1", "id": "prt_b",
                 "state": {"status": "running", "input": {"command": "ls"}}},
    })
    completed = _norm({
        "type": "tool_use", "sessionID": _SID,
        "part": {"type": "tool", "tool": "bash", "callID": "b1", "id": "prt_b",
                 "state": {"status": "completed", "input": {"command": "ls"},
                           "output": "file.py"}},
    })
    assert len(running) == 1
    assert len(completed) == 2
    # Same part → same tool_use render uuid, so the completed update
    # replaces the running one instead of duplicating.
    assert running[0]["uuid"] == completed[0]["uuid"]


def test_runner_error_tool_result_flagged() -> None:
    out = _norm({
        "type": "tool_use", "sessionID": _SID,
        "part": {"type": "tool", "tool": "bash", "callID": "b2", "id": "prt_e",
                 "state": {"status": "error", "input": {"command": "boom"},
                           "error": "command failed"}},
    })
    assert len(out) == 2
    block = out[1]["message"]["content"][0]
    assert block["is_error"] is True
    assert block["content"] == "command failed"


def test_runner_skips_bookkeeping_events() -> None:
    for etype in ("step_start", "step_finish"):
        out = _norm({
            "type": etype, "sessionID": _SID,
            "part": {"id": "prt_s", "type": etype.replace("_", "-"),
                     "tokens": {"total": 10, "input": 8, "output": 2,
                                "reasoning": 0, "cache": {"write": 0, "read": 0}}},
        })
        assert not out


def test_runner_surfaces_unknown_event() -> None:
    out = _norm({"type": "brand_new_thing", "sessionID": _SID, "part": {"id": "p"}})
    assert len(out) == 1
    assert out[0]["type"] == "unknown_event"
    assert out[0]["raw_type"] == "brand_new_thing"


def test_runner_uuid_deterministic_per_part() -> None:
    event = {
        "type": "text", "sessionID": _SID,
        "part": {"id": "prt_x", "type": "text", "text": "partial"},
    }
    grown = {
        "type": "text", "sessionID": _SID,
        "part": {"id": "prt_x", "type": "text", "text": "partial then more"},
    }
    a = _norm(event)[0]
    b = _norm(grown)[0]
    other = _norm({
        "type": "text", "sessionID": _SID,
        "part": {"id": "prt_y", "type": "text", "text": "different part"},
    })[0]
    assert a["uuid"] == b["uuid"]
    assert a["uuid"] != other["uuid"]


def test_runner_usage_accumulation() -> None:
    usage: dict[str, int] = {}
    usage = runner_opencode._sum_tokens(usage, {
        "total": 15387, "input": 15301, "output": 0, "reasoning": 94,
        "cache": {"write": 0, "read": 5},
    })
    usage = runner_opencode._sum_tokens(usage, {
        "total": 100, "input": 80, "output": 20, "reasoning": 0,
        "cache": {"write": 2, "read": 0},
    })
    assert usage == {
        "input_tokens": 15381,
        "output_tokens": 20,
        "total_tokens": 15487,
        "reasoning_tokens": 94,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 2,
    }


def test_capability_context_labels_team_message() -> None:
    from capability_contexts import prompt_heading_for_source

    assert prompt_heading_for_source("mssg") == "Message"
    assert prompt_heading_for_source("team_ask") == "Ask"


TESTS = [
    ("kind_and_capability_matrix", test_kind_and_capability_matrix),
    ("build_env_scrubs_claude_session_env", test_build_env_scrubs_claude_session_env),
    ("models_static_seed", test_models_static_seed),
    ("parses_models_output", test_parses_models_output),
    ("models_fetch_parses_real_cli", test_models_fetch_parses_real_cli),
    ("argv_stdin_only_no_prompt_leakage", test_argv_stdin_only_no_prompt_leakage),
    ("argv_resume_fork_variant_files", test_argv_resume_fork_variant_files),
    ("argv_fork_without_session_fails_closed", test_argv_fork_without_session_fails_closed),
    ("permission_mapping", test_permission_mapping),
    ("permission_unknown_mode_fails_closed", test_permission_unknown_mode_fails_closed),
    ("runner_normalizes_text_event", test_runner_normalizes_text_event),
    ("runner_normalizes_reasoning_event", test_runner_normalizes_reasoning_event),
    ("runner_normalizes_completed_tool_event", test_runner_normalizes_completed_tool_event),
    ("runner_running_tool_then_completion_dedup", test_runner_running_tool_emits_use_only_then_result_on_completion),
    ("runner_error_tool_result_flagged", test_runner_error_tool_result_flagged),
    ("runner_skips_bookkeeping_events", test_runner_skips_bookkeeping_events),
    ("runner_surfaces_unknown_event", test_runner_surfaces_unknown_event),
    ("runner_uuid_deterministic_per_part", test_runner_uuid_deterministic_per_part),
    ("runner_usage_accumulation", test_runner_usage_accumulation),
    ("capability_context_labels_team_message", test_capability_context_labels_team_message),
]


def main() -> None:
    failures = []
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError as exc:
                print(f"FAIL: {name} ({exc})")
                failures.append(name)
                continue
            except Exception:  # noqa: BLE001 — report, don't abort the suite
                import traceback
                print(f"FAIL: {name}")
                traceback.print_exc()
                failures.append(name)
                continue
            print(f"PASS: {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
