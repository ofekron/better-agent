"""Locks the origin-labeled continuation handoff prompt.

A continuation subprocess receives a carried-over message that may be a
terse fragment ("verified fixed"). The handoff MUST (1) label that block
verbatim, (2) name whether the USER or an AGENT authored it, and (3) tell
the continuation to reconstruct missing context before acting. Regression
guard for the failure where a fresh subprocess treated a two-word user
message + a dirty working tree as the whole task.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="continuation_origin_home_")
paths.engage_test_home(_TMP)

from continuation import (  # noqa: E402
    build_continuation_prompt,
    normalize_prompt_origin,
)

_RECONSTRUCT = "Reconstruct that context yourself before acting"


def test_user_origin_labels_user() -> None:
    prompt = build_continuation_prompt(
        prompt="verified fixed",
        app_session_id="s1",
        continuation_chain=[],
        reason="selector_changed",
        origin="user",
    )
    assert "BEGIN VERBATIM USER MESSAGE" in prompt
    assert "END VERBATIM USER MESSAGE" in prompt
    assert "BEGIN VERBATIM AGENT MESSAGE" not in prompt
    assert "the USER typed" in prompt
    assert "verified fixed" in prompt
    assert _RECONSTRUCT in prompt


def test_agent_origin_labels_agent() -> None:
    prompt = build_continuation_prompt(
        prompt="continue the migration",
        app_session_id="s1",
        continuation_chain=[],
        reason="agent_requested",
        origin="agent",
    )
    assert "BEGIN VERBATIM AGENT MESSAGE" in prompt
    assert "an AGENT queued" in prompt
    assert "BEGIN VERBATIM USER MESSAGE" not in prompt
    assert "continue the migration" in prompt
    assert _RECONSTRUCT in prompt


def test_default_origin_is_user() -> None:
    prompt = build_continuation_prompt(
        prompt="x", app_session_id="s1", continuation_chain=[],
    )
    assert "BEGIN VERBATIM USER MESSAGE" in prompt


def test_normalize_prompt_origin() -> None:
    assert normalize_prompt_origin("agent") == "agent"
    assert normalize_prompt_origin("AGENT") == "agent"
    assert normalize_prompt_origin("user") == "user"
    assert normalize_prompt_origin("") == "user"
    assert normalize_prompt_origin(None) == "user"
    assert normalize_prompt_origin("nonsense") == "user"


def test_carried_message_is_not_re_substituted() -> None:
    # A carried message containing template-looking text must survive
    # verbatim — the builder substitutes values, it must not re-scan them.
    payload = "check $app_session_id and $prompt literally"
    prompt = build_continuation_prompt(
        prompt=payload, app_session_id="realsid", continuation_chain=[],
        origin="user",
    )
    assert payload in prompt
    # The literal "$app_session_id" token from the payload stayed literal
    # (was not replaced by "realsid").
    assert "check $app_session_id and $prompt literally" in prompt


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok - {name}")
    print("PASS test_continuation_prompt_origin")


if __name__ == "__main__":
    _run()
