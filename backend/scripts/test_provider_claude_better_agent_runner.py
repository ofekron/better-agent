"""Unit coverage for the instance methods of ClaudeBetterAgentRunnerProvider
(the claude+better_agent_runner delegate in provider_claude_better_agent_runner.py).

Scope: build_env (foreign-auth-var scrub), steer_run (every file-I/O branch),
run_headless (unimplemented stub contract), and _start_run (delegation+return).

The two heavy module-level assembly functions (prepare_better_agent_runner_run,
_build_better_agent_runner_input) need a full provider/config/session/extension
stack and belong to the full-stack tier; they are intentionally not covered here.
ClaudeProvider's dispatch *into* this delegate is already locked by
test_claude_better_agent_runner_dispatch.py.

Run:
    cd backend && .venv/bin/python -m pytest scripts/test_provider_claude_better_agent_runner.py \
        --cov=provider_claude_better_agent_runner --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from provider_claude_better_agent_runner import (  # noqa: E402
    ClaudeBetterAgentRunnerProvider,
)
from provider_session_events import RunState  # noqa: E402

# Every foreign-provider auth var build_env must scrub, exactly as enumerated
# in the source. Kept as an explicit literal (not imported) so a source edit
# that drops/renames one of these is caught by the scrub test.
_FOREIGN_AUTH_VARS = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "GEMINI_CLI_HOME",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


def _make_provider() -> ClaudeBetterAgentRunnerProvider:
    """Minimal record: Provider.__init__ only requires record["id"];
    require_runtime_credential is mocked per-test where build_env is exercised
    so the credential-state path never determines env-scrub behavior."""
    return ClaudeBetterAgentRunnerProvider(
        {"id": "ba-runner-unit", "kind": "claude", "mode": "subscription"},
    )


class _FakePopen:
    """Stand-in for a subprocess.Popen: only poll() (liveness) is exercised
    by steer_run. A dead process reports a non-None returncode."""

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self.returncode = None if alive else 0

    def poll(self):
        return self.returncode


def _register_run(provider, tmp_path, *, run_id="run-1", alive=True):
    """Inject a RunState into the provider's run registry pointing at a real
    tmp run_dir. Returns the run_dir so the caller can seed state.json."""
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    provider._runs[run_id] = RunState(
        run_id=run_id,
        run_dir=run_dir,
        popen=_FakePopen(alive=alive),
        mode="native",
        app_session_id="app-session-1",
        queue=asyncio.Queue(),
    )
    return run_dir


# ---------------------------------------------------------------------------
# build_env — credential gate + foreign-auth-var scrub
# ---------------------------------------------------------------------------
def test_build_env_scrubs_every_foreign_provider_auth_var():
    provider = _make_provider()
    seed = {var: f"value-{var}" for var in _FOREIGN_AUTH_VARS}
    seed["PATH_KEEP_ME"] = "preserved"
    with mock.patch.object(provider, "require_runtime_credential") as cred_check:
        with mock.patch.dict(os.environ, seed, clear=False):
            env = provider.build_env()
    # Credential gate fired before any env work.
    cred_check.assert_called_once_with()
    # Every foreign auth var is gone.
    for var in _FOREIGN_AUTH_VARS:
        assert var not in env, f"{var} leaked into built env"
    # Unrelated env survives.
    assert env["PATH_KEEP_ME"] == "preserved"


def test_build_env_fails_closed_when_credential_unavailable():
    """Security: env is never returned when the credential gate rejects."""
    from provider import ProviderCredentialError

    provider = _make_provider()
    with mock.patch.object(
        provider,
        "require_runtime_credential",
        side_effect=ProviderCredentialError(
            "no credential", provider_id=provider.id, credential_status="missing",
        ),
    ):
        try:
            provider.build_env()
        except ProviderCredentialError:
            pass
        else:
            raise AssertionError("build_env must propagate the credential rejection")


# ---------------------------------------------------------------------------
# steer_run — every branch
# ---------------------------------------------------------------------------
def test_steer_run_unknown_run_returns_false(tmp_path):
    provider = _make_provider()
    assert provider.steer_run("absent-run", "do something") is False


def test_steer_run_dead_popen_returns_false(tmp_path):
    provider = _make_provider()
    _register_run(provider, tmp_path, run_id="dead", alive=False)
    # run_dir has a valid state.json — proves the popen-dead gate fires first.
    assert provider.steer_run("dead", "do something") is False


def test_steer_run_empty_prompt_and_no_images_returns_false(tmp_path):
    provider = _make_provider()
    _register_run(provider, tmp_path, run_id="empty")
    assert provider.steer_run("empty", "   ") is False
    assert provider.steer_run("empty", "", images=[]) is False


def test_steer_run_missing_state_file_returns_false(tmp_path):
    provider = _make_provider()
    _register_run(provider, tmp_path, run_id="nostate")
    # No state.json written -> OSError on read.
    assert provider.steer_run("nostate", "do something") is False


def test_steer_run_corrupt_state_json_returns_false(tmp_path):
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="corrupt")
    (run_dir / "state.json").write_text("{not valid json", encoding="utf-8")
    assert provider.steer_run("corrupt", "do something") is False


def test_steer_run_state_without_session_id_returns_false(tmp_path):
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="nosid")
    (run_dir / "state.json").write_text(
        json.dumps({"other": "value"}), encoding="utf-8",
    )
    assert provider.steer_run("nosid", "do something") is False


def test_steer_run_success_appends_prompt_to_steer_jsonl(tmp_path):
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="ok")
    (run_dir / "state.json").write_text(
        json.dumps({"session_id": "sess-123"}), encoding="utf-8",
    )

    assert provider.steer_run("ok", "turn left", images=None) is True

    lines = (run_dir / "steer.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["prompt"] == "turn left"
    assert entry["images"] == []


def test_steer_run_success_carries_images(tmp_path):
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="img")
    (run_dir / "state.json").write_text(
        json.dumps({"session_id": "sess-456"}), encoding="utf-8",
    )

    assert provider.steer_run("img", "look", images=["data:img/png;base64,abc"]) is True
    entry = json.loads((run_dir / "steer.jsonl").read_text(encoding="utf-8"))
    assert entry["images"] == ["data:img/png;base64,abc"]


def test_steer_run_success_appends_multiple_in_order(tmp_path):
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="multi")
    (run_dir / "state.json").write_text(
        json.dumps({"session_id": "sess-789"}), encoding="utf-8",
    )

    assert provider.steer_run("multi", "first") is True
    assert provider.steer_run("multi", "second") is True

    prompts = [
        json.loads(line)["prompt"]
        for line in (run_dir / "steer.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert prompts == ["first", "second"]


def test_steer_run_write_failure_returns_false(tmp_path):
    """run_dir is read-only after a valid state.json is in place, so the
    state read succeeds but creating steer.jsonl raises PermissionError
    (an OSError), which steer_run swallows and reports as False."""
    provider = _make_provider()
    run_dir = _register_run(provider, tmp_path, run_id="ro")
    (run_dir / "state.json").write_text(
        json.dumps({"session_id": "sess-ro"}), encoding="utf-8",
    )
    os.chmod(run_dir, 0o500)  # r-x: owner cannot create files
    try:
        assert provider.steer_run("ro", "do something") is False
    finally:
        os.chmod(run_dir, 0o755)  # restore so tmp cleanup can rm


# ---------------------------------------------------------------------------
# run_headless — unimplemented-stub contract
# ---------------------------------------------------------------------------
def test_run_headless_returns_none_and_does_not_raise():
    provider = _make_provider()
    result = asyncio.run(
        provider.run_headless(
            prompt="hi",
            session_id=None,
            resume_sid=None,
            fork=False,
            cwd=None,
            timeout=None,
            no_tools=False,
        ),
    )
    assert result is None


# ---------------------------------------------------------------------------
# _start_run — delegates to start_session_events_execution and returns True
# ---------------------------------------------------------------------------
def test_start_run_delegates_and_returns_true():
    provider = _make_provider()
    sentinel_execution = object()
    sentinel_loop = object()
    sentinel_queue = object()
    with mock.patch.object(
        provider, "start_session_events_execution",
    ) as delegated:
        ok = provider._start_run(
            loop=sentinel_loop,
            queue=sentinel_queue,
            internal_token="tok",
            extra_env={"X": "1"},
            _execution=sentinel_execution,
            unrelated_kwarg="ignored",
        )
    assert ok is True
    delegated.assert_called_once_with(
        execution=sentinel_execution,
        loop=sentinel_loop,
        queue=sentinel_queue,
        internal_token="tok",
        extra_env={"X": "1"},
    )
