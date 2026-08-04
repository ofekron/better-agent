"""Full unit coverage for ``claude_subscription_credential.py``.

``read_claude_subscription_token`` shells out to the macOS ``security`` CLI
to read the Claude Code OAuth token from Keychain. The module is
dependency-light (``json`` + ``subprocess`` + ``logging`` only) so every
branch is exercised deterministically by monkeypatching
``subprocess.run`` — no real Keychain, no network, no macOS dependency.
"""

from __future__ import annotations

import json
import subprocess
import sys
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import claude_subscription_credential as csc  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture
def patch_run(monkeypatch):
    """Replace subprocess.run inside the module with a controllable stub.

    Yields a mutator so each test sets the desired behavior, then triggers a
    read. Default behavior raises so a forgotten configuration fails loudly.
    """

    holder: dict[str, object] = {}

    def _fake_run(*args, **kwargs):
        behavior = holder.get("behavior")
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    monkeypatch.setattr(csc.subprocess, "run", _fake_run)
    return holder


def test_success_returns_access_token(patch_run):
    patch_run["behavior"] = _FakeCompleted(
        stdout=json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-test-token", "other": 1}}
        ),
        returncode=0,
    )
    assert csc.read_claude_subscription_token() == "sk-test-token"


def test_timeout_returns_none(patch_run):
    patch_run["behavior"] = subprocess.TimeoutExpired(cmd="security", timeout=5)
    assert csc.read_claude_subscription_token() is None


def test_filenotfound_returns_none(patch_run):
    # Raised when the `security` binary is absent (not macOS).
    patch_run["behavior"] = FileNotFoundError("[Errno 2] No such file")
    assert csc.read_claude_subscription_token() is None


def test_generic_subprocess_error_returns_none(patch_run):
    patch_run["behavior"] = subprocess.SubprocessError("boom")
    assert csc.read_claude_subscription_token() is None


def test_nonzero_returncode_returns_none(patch_run):
    patch_run["behavior"] = _FakeCompleted(stdout="", returncode=44)
    assert csc.read_claude_subscription_token() is None


def test_success_zero_returncode_with_empty_stdout_then_json_error(patch_run):
    # returncode 0 but unparseable stdout -> JSONDecodeError branch.
    patch_run["behavior"] = _FakeCompleted(stdout="not json", returncode=0)
    assert csc.read_claude_subscription_token() is None


def test_missing_claude_ai_oauth_key_returns_none(patch_run):
    patch_run["behavior"] = _FakeCompleted(
        stdout=json.dumps({"unrelated": {}}), returncode=0
    )
    assert csc.read_claude_subscription_token() is None


def test_missing_access_token_key_returns_none(patch_run):
    patch_run["behavior"] = _FakeCompleted(
        stdout=json.dumps({"claudeAiOauth": {"refreshToken": "x"}}), returncode=0
    )
    assert csc.read_claude_subscription_token() is None


def test_stdout_not_a_mapping_returns_none(patch_run):
    # json.loads("123") -> int; data["claudeAiOauth"] raises TypeError.
    patch_run["behavior"] = _FakeCompleted(stdout="123", returncode=0)
    assert csc.read_claude_subscription_token() is None


def test_kwargs_forwarded_to_security_cli(monkeypatch):
    captured: dict[str, object] = {}

    def _capture_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted(
            stdout=json.dumps({"claudeAiOauth": {"accessToken": "t"}}),
            returncode=0,
        )

    monkeypatch.setattr(csc.subprocess, "run", _capture_run)
    assert csc.read_claude_subscription_token() == "t"
    assert captured["args"] == [
        "security", "find-generic-password",
        "-s", "Claude Code-credentials", "-w",
    ]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["timeout"] == 5
