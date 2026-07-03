"""Focused tests for the Copilot provider.

Pins:
  1. Registry: `_resolve_class("copilot")` → CopilotProvider, KIND="copilot".
  2. Capability matrix: native-only (no fork / team / steering / reasoning),
     simulated rewind on.
  3. Models: static cold-start seed + `_resolve_refresh_fetch` dispatches to
     `fetch_copilot_models` (and the real CLI parses if installed).
  4. Setup: copilot is in the installer map with a platform install argv and
     `copilot --version` verify.
  5. Runner event normalization: each Copilot session-state event type maps
     to the correct Claude-shaped agent_message (user text, assistant text,
     tool_use, tool_result) and re-normalizing the same event is idempotent
     on the render uuid.

Run:
    cd backend && .venv/bin/python scripts/test_provider_copilot.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-copilot-")

import provider_copilot  # noqa: E402
import runner_copilot  # noqa: E402
from provider import _resolve_class  # noqa: E402
import models  # noqa: E402
import provider_setup  # noqa: E402


def test_registry_resolves_copilot() -> bool:
    cls = _resolve_class("copilot")
    return cls is provider_copilot.CopilotProvider and cls.KIND == "copilot"


def test_capability_matrix() -> bool:
    cls = _resolve_class("copilot")
    expected = {
        "supports_fork": False,
        "supports_manager_mode": False,
        "supports_rewind": True,
        "supports_steering": False,
        "supports_native_subagents": False,
        "supports_reasoning_effort": False,
    }
    return all(getattr(cls, k) is v for k, v in expected.items())


def test_build_env_clears_anthropic() -> bool:
    inst = provider_copilot.CopilotProvider({"id": "c1", "kind": "copilot", "mode": "subscription"})
    env = inst.build_env()
    return not any(k in env for k in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    ))


def test_models_static_seed() -> bool:
    seeded = models._static_cold_start({"kind": "copilot"})
    return bool(seeded) and "auto" in seeded and "gpt-5.4" in seeded


def test_models_refresh_dispatch() -> bool:
    fetcher = models._resolve_refresh_fetch({"kind": "copilot"})
    return callable(fetcher)


def test_models_fetch_parses_real_cli() -> bool:
    # Only assert when the copilot CLI is installed on PATH; otherwise skip
    # (the static seed covers cold-start).
    if not shutil.which("copilot"):
        return True
    parsed = provider_copilot.fetch_copilot_models()
    return (
        bool(parsed)
        and "auto" in parsed
        and "gpt-5.4" in parsed
        and len(parsed) >= 5
    )


def test_parses_config_help_models() -> bool:
    sample = """
Configuration Settings:

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag option.
    - "claude-sonnet-4.6"
    - "gpt-5.4"
    - "gpt-5.3-codex"
    - "gpt-5-mini"

  `contextTier`: context window tier for tiered-pricing models.
"""
    return provider_copilot._parse_copilot_config_models(sample) == [
        "auto",
        "claude-sonnet-4.6",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5-mini",
        "mai-code-1-flash-picker",
    ]


def test_parses_legacy_help_choices() -> bool:
    sample = """
Options:
  --model <model>  Set the AI model (choices: "gpt-5.4", "claude-sonnet-4.6", "gemma-3")
"""
    return provider_copilot._parse_copilot_help_choices(sample) == [
        "auto",
        "gpt-5.4",
        "claude-sonnet-4.6",
    ]


def test_retired_model_fallbacks_to_auto() -> bool:
    return provider_copilot._normalize_copilot_model("gpt-5.2-codex") == "auto"


def test_setup_installer() -> bool:
    kinds = provider_setup.supported_provider_kinds()
    if "copilot" not in kinds:
        return False
    inst = provider_setup.installer_for("copilot")
    expected_prefix = ("winget", "install") if sys.platform == "win32" else ("brew", "install")
    return (
        inst.kind == "copilot"
        and inst.command == "copilot"
        and inst.verify_argv == ("copilot", "--version")
        and inst.install_argv[:2] == expected_prefix
    )


def test_runner_normalizes_event_types() -> bool:
    sid = "000223ae-8f80-472a-84ad-f6951f71887f"
    cases = [
        (
            {"type": "user.message", "data": {"content": "hi"}, "id": "u1", "timestamp": "t"},
            "user", "text", "hi",
        ),
        (
            {"type": "assistant.message", "data": {"content": "hello", "messageId": "m1"}, "id": "a1", "timestamp": "t"},
            "assistant", "text", "hello",
        ),
        (
            {"type": "tool.execution_start",
             "data": {"toolCallId": "call_1", "toolName": "ls", "arguments": {"path": "."}},
             "id": "t1", "timestamp": "t"},
            "assistant", "tool_use", "ls",
        ),
        (
            {"type": "tool.execution_complete",
             "data": {"toolCallId": "call_1", "success": True, "result": {"content": "file.py"}},
             "id": "t2", "timestamp": "t"},
            "user", "tool_result", "file.py",
        ),
    ]
    for event, role, block_type, payload in cases:
        out = runner_copilot.normalize_copilot_event(
            event, session_id=sid, parent_uuid=sid, model="gpt-5.4",
        )
        if out is None or out["type"] != "agent_message":
            return False
        data = out["data"]
        if data["type"] != role or data["parentUuid"] != sid:
            return False
        block = data["message"]["content"][0]
        if block["type"] != block_type:
            return False
        if block_type == "text" and block["text"] != payload:
            return False
        if block_type == "tool_use" and (block["name"] != payload or block["id"] != "call_1"):
            return False
        if block_type == "tool_result" and (block["content"] != payload or block["tool_use_id"] != "call_1"):
            return False
    return True


def test_runner_normalizer_skips_bookkeeping() -> bool:
    for etype in ("session.start", "assistant.turn_start", "assistant.turn_end", "session.truncation"):
        out = runner_copilot.normalize_copilot_event(
            {"type": etype, "data": {}, "id": "x", "timestamp": "t"},
            session_id="s", parent_uuid="s", model="copilot",
        )
        if out is not None:
            return False
    return True


def test_runner_uuid_is_deterministic() -> bool:
    event = {"type": "assistant.message", "data": {"content": "x", "messageId": "m"},
             "id": "evt-1", "timestamp": "t"}
    a = runner_copilot.normalize_copilot_event(
        event, session_id="s1", parent_uuid="s1", model="copilot")
    b = runner_copilot.normalize_copilot_event(
        event, session_id="s1", parent_uuid="s1", model="copilot")
    return a["data"]["uuid"] == b["data"]["uuid"]


def test_capability_context_labels_team_message() -> bool:
    prompt = runner_copilot._prepend_capability_context("<mssg>done</mssg>", {
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
    ("registry_resolves_copilot", test_registry_resolves_copilot),
    ("capability_matrix", test_capability_matrix),
    ("build_env_clears_anthropic", test_build_env_clears_anthropic),
    ("models_static_seed", test_models_static_seed),
    ("models_refresh_dispatch", test_models_refresh_dispatch),
    ("models_fetch_parses_real_cli", test_models_fetch_parses_real_cli),
    ("parses_config_help_models", test_parses_config_help_models),
    ("parses_legacy_help_choices", test_parses_legacy_help_choices),
    ("retired_model_fallbacks_to_auto", test_retired_model_fallbacks_to_auto),
    ("setup_installer", test_setup_installer),
    ("runner_normalizes_event_types", test_runner_normalizes_event_types),
    ("runner_normalizer_skips_bookkeeping", test_runner_normalizer_skips_bookkeeping),
    ("runner_uuid_is_deterministic", test_runner_uuid_is_deterministic),
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
