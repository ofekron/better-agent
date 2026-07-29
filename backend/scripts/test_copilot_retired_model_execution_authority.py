"""Regression test for a model-normalization / execution-authority split.

Bug (found 2026-07-29 while live-verifying every configured provider):
Copilot turns with a retired default model (e.g. "gpt-5.2-codex", which
`_normalize_copilot_model` collapses to "auto") failed at run start with
`ExecutionAuthorityError: recovered run input conflicts with execution
authority: model`.

Root cause: `provider_session_events_execution.prepare_session_events_execution`
computed `runner_input["model"]` via `_normalize_model` (which can rewrite the
value for copilot's retired-model fallbacks), but froze the execution
template from the RAW, pre-normalization `start_arguments["model"]`. The
frozen template and the actual runner input then disagreed, and
`execution_artifact_io.bind_execution_input`'s exact-match check on "model"
rejected the run.

Fix: `prepare_session_events_execution` now builds `start_arguments` for the
frozen template using `runner_input["model"]` (the final, already-normalized
value), so both paths agree by construction.

This test exercises the underlying invariant directly via
`execution_template.prepare_execution` + `execution_artifact_io.bind_execution_input`
(no real CLI/session_manager machinery needed), and separately pins the fix's
exact source shape so a regression to `dict(start_arguments)` is caught.

Run:
    cd backend && .venv/bin/python scripts/test_copilot_retired_model_execution_authority.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-copilot-retired-model-")

from execution_artifact_io import bind_execution_input  # noqa: E402
from execution_template import ExecutionAuthorityError, prepare_execution  # noqa: E402
import provider_copilot  # noqa: E402


_PROVIDER = {
    "id": "copilot-provider",
    "kind": "copilot",
    "generation": str(uuid.uuid4()),
    "revision": 0,
}

_REQUIRED_ARGUMENTS = {
    "run_id": "run-1",
    "prompt": "hi",
    "cwd": "/tmp",
    "reasoning_effort": None,
    "session_id": "session-1",
    "mode": "native",
    "app_session_id": "app-session-1",
}

_PROJECTION_BASE = {
    "app_session_id": "app-session-1",
    "cwd": "/tmp",
    "mode": "native",
    "prompt": "hi",
    "provider_id": "copilot-provider",
    "session_id": "session-1",
}


def _artifact_for_model(model: str):
    execution = prepare_execution(
        _PROVIDER,
        model=model,
        **_REQUIRED_ARGUMENTS,
    )
    return execution.artifact


def _projection_for_model(model: str) -> dict:
    return {**_PROJECTION_BASE, "model": model}


def test_retired_model_normalizes_to_auto() -> None:
    # Pin the substitution this whole bug hinges on.
    assert provider_copilot._normalize_copilot_model("gpt-5.2-codex") == "auto"


def test_frozen_raw_model_conflicts_with_normalized_runner_input() -> None:
    # Reproduces the pre-fix bug: template frozen with the RAW retired
    # model, then bound against the NORMALIZED runner input the actual run
    # would use. This is exactly the split prepare_session_events_execution
    # used to produce.
    artifact = _artifact_for_model("gpt-5.2-codex")
    try:
        bind_execution_input(artifact, _projection_for_model("auto"))
    except ExecutionAuthorityError as exc:
        assert "model" in str(exc)
    else:
        raise AssertionError(
            "expected a raw/normalized model mismatch to fail closed",
        )


def test_frozen_normalized_model_matches_runner_input() -> None:
    # Reproduces the fix: template frozen with the SAME already-normalized
    # model the runner input uses. No conflict.
    artifact = _artifact_for_model("auto")
    bound = bind_execution_input(artifact, _projection_for_model("auto"))
    assert bound["model"] == "auto"


def test_prepare_session_events_execution_freezes_normalized_model() -> None:
    source = (
        _BACKEND / "provider_session_events_execution.py"
    ).read_text(encoding="utf-8")
    assert 'start_arguments={**start_arguments, "model": runner_input["model"]}' in source
    assert "start_arguments=dict(start_arguments)," not in source


TESTS = (
    test_retired_model_normalizes_to_auto,
    test_frozen_raw_model_conflicts_with_normalized_runner_input,
    test_frozen_normalized_model_matches_runner_input,
    test_prepare_session_events_execution_freezes_normalized_model,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS copilot retired-model execution authority")
