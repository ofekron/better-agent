"""Tests for runner_errors — the consolidated provider-runner error
classification table — and the resume session-loss guard wired into the
pi / qwen / cursor / amp / opencode runners (kimi's stream reports no
session id, so it has no guard).

Run:
    cd backend && .venv/bin/python scripts/test_runner_errors.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-runner-errors-")

import runner_errors  # noqa: E402
from runner_errors import (  # noqa: E402
    CATEGORY_AUTH,
    CATEGORY_NETWORK,
    CATEGORY_QUOTA_RATE_LIMIT,
    CATEGORY_SESSION_LOST,
    classify,
    extract_stderr_error,
    resume_session_mismatch,
    stderr_error,
)


# ============================================================================
# Classification table
# ============================================================================
def test_pi_auth_rule() -> bool:
    hit = classify("pi", "No API key found for provider anthropic")
    split = classify("pi", "Use /login first.\nSelect a provider to continue.")
    return (
        hit is not None and hit.category == CATEGORY_AUTH
        and "/login" in hit.message
        and split is not None and split.category == CATEGORY_AUTH
    )


def test_qwen_session_lost_rule() -> bool:
    hit = classify(
        "qwen",
        "No saved session found with ID 123e4567-e89b-12d3-a456-426614174000. "
        "Run `qwen --resume` without an ID to choose from existing sessions.",
    )
    return (
        hit is not None
        and hit.category == CATEGORY_SESSION_LOST
        and "session" in hit.message.lower()
    )


def test_qwen_auth_rule() -> bool:
    hit = classify("qwen", "Error: No auth type is selected.")
    return hit is not None and hit.category == CATEGORY_AUTH and "OPENAI_API_KEY" in hit.message


def test_cursor_auth_rule() -> bool:
    hit = classify("cursor", "Error: Not authenticated. Run cursor-agent login")
    return hit is not None and hit.category == CATEGORY_AUTH and "cursor-agent login" in hit.message


def test_amp_rules() -> bool:
    auth = classify("amp", "Error: API key is not configured. Run `amp login`.")
    quota = classify("amp", "402 Payment Required: insufficient credits for this request")
    return (
        auth is not None and auth.category == CATEGORY_AUTH
        and quota is not None and quota.category == CATEGORY_QUOTA_RATE_LIMIT
    )


def test_common_rules_apply_to_kimi_and_opencode() -> bool:
    net = classify("kimi", "fetch failed: connect ECONNREFUSED 127.0.0.1:443")
    quota = classify("opencode", "Provider error: rate limit exceeded, retry later")
    auth = classify("opencode", "Error: invalid API key provided")
    return (
        net is not None and net.category == CATEGORY_NETWORK
        and quota is not None and quota.category == CATEGORY_QUOTA_RATE_LIMIT
        and auth is not None and auth.category == CATEGORY_AUTH
    )


def test_first_match_wins_provider_before_common() -> bool:
    # Text matches both cursor's auth rule and the common network rule;
    # the provider-specific auth rule is ordered first.
    hit = classify("cursor", "authentication required\nsocket hang up")
    return hit is not None and hit.category == CATEGORY_AUTH


def test_matched_line_and_message_fallback() -> bool:
    hit = classify("kimi", "some preamble\nHTTP 503 service unavailable\ntrailer")
    return (
        hit is not None
        and hit.matched == "HTTP 503 service unavailable"
        and hit.message == hit.matched
    )


def test_unknown_kind_fails_closed() -> bool:
    try:
        classify("nonexistent-kind", "boom")
    except ValueError:
        return True
    return False


def test_no_match_and_empty_return_none() -> bool:
    return classify("pi", "all fine here") is None and classify("pi", "", None) is None


# ============================================================================
# stderr extraction fallback
# ============================================================================
def test_extract_stderr_error_named_error_first() -> bool:
    text = "warning: something\nTypeError: cannot read x\n    at foo (bar.js:1)\ndone"
    return extract_stderr_error(text) == "TypeError: cannot read x"


def test_extract_stderr_error_error_prefix_then_last_line() -> bool:
    prefixed = extract_stderr_error("noise\nerror: it broke\nmore noise")
    last = extract_stderr_error("first line\n    at frame (x.js:1)\nlast meaningful line\n}")
    return prefixed == "error: it broke" and last == "last meaningful line"


def test_stderr_error_prefers_classification() -> bool:
    friendly = stderr_error("pi", "No API key found for provider openai")
    raw = stderr_error("pi", "some unclassified failure line")
    return (
        friendly is not None and "/login" in friendly
        and raw == "some unclassified failure line"
        and stderr_error("pi", "") is None
    )


# ============================================================================
# Resume session-loss guard (pure function)
# ============================================================================
def test_resume_session_mismatch() -> bool:
    hit = resume_session_mismatch("cursor", "chat-a", "chat-b")
    same = resume_session_mismatch("cursor", "chat-a", "chat-a")
    fresh = resume_session_mismatch("cursor", "", "chat-b")
    unobserved = resume_session_mismatch("cursor", "chat-a", None)
    return (
        hit is not None
        and hit.category == CATEGORY_SESSION_LOST
        and "chat-a" in hit.message and "chat-b" in hit.message
        and same is None and fresh is None and unobserved is None
    )


# ============================================================================
# Resume-mismatch guard wired into the runners (fake CLI stream fixtures)
# ============================================================================
def _make_fake_cli(bin_dir: Path, name: str, lines: list[str], *, then_sleep: bool = True) -> None:
    script = bin_dir / name
    body = ["#!/bin/sh", "cat > /dev/null 2>&1 || true"]
    for line in lines:
        body.append(f"echo '{line}'")
    if then_sleep:
        body.append("sleep 30")
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run_with_fake_cli(
    runner_module: str,
    cli_name: str,
    lines: list[str],
    inputs: dict,
    *,
    then_sleep: bool = True,
) -> dict:
    """Run <runner_module>._run against a fake CLI that prints `lines`;
    returns the parsed complete.json. `then_sleep` keeps the fake CLI
    alive after printing, proving the runner kills it on session loss."""
    import importlib
    runner = importlib.import_module(runner_module)
    work = Path(tempfile.mkdtemp(prefix="runner-errors-fixture-"))
    bin_dir = work / "bin"
    bin_dir.mkdir(parents=True)
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    _make_fake_cli(bin_dir, cli_name, lines, then_sleep=then_sleep)
    inputs = {"cwd": str(work), "mode": "native", "app_session_id": "app-1", **inputs}
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        asyncio.run(runner._run(run_dir, inputs))
        return json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(work, ignore_errors=True)


def _is_session_lost_failure(complete: dict, requested: str) -> bool:
    return (
        complete.get("success") is False
        and "session lost" in str(complete.get("error") or "")
        and requested in str(complete.get("error") or "")
    )


def test_cursor_resume_mismatch_fails_run() -> bool:
    complete = _run_with_fake_cli(
        "runner_cursor", "cursor-agent",
        ['{"type":"system","subtype":"init","session_id":"other-chat","model":"m"}'],
        {"prompt": "hi", "session_id": "requested-chat"},
    )
    return _is_session_lost_failure(complete, "requested-chat")


def test_cursor_resume_same_sid_passes() -> bool:
    complete = _run_with_fake_cli(
        "runner_cursor", "cursor-agent",
        [
            '{"type":"system","subtype":"init","session_id":"requested-chat","model":"m"}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"ok","session_id":"requested-chat"}',
        ],
        {"prompt": "hi", "session_id": "requested-chat"},
        then_sleep=False,
    )
    return complete.get("success") is True and complete.get("session_id") == "requested-chat"


def test_opencode_resume_mismatch_fails_run() -> bool:
    complete = _run_with_fake_cli(
        "runner_opencode", "opencode",
        ['{"type":"text","sessionID":"other-session","part":{"id":"p1","text":"hi"}}'],
        {"prompt": "hi", "session_id": "requested-session"},
    )
    return _is_session_lost_failure(complete, "requested-session")


def test_opencode_fork_new_sid_allowed() -> bool:
    complete = _run_with_fake_cli(
        "runner_opencode", "opencode",
        [
            '{"type":"text","sessionID":"forked-session","part":{"id":"p1","text":"hello"}}',
        ],
        {"prompt": "hi", "session_id": "requested-session", "fork": True},
        then_sleep=False,
    )
    # Fork legitimately reports a new session id — no session_lost error.
    return "session lost" not in str(complete.get("error") or "")


def test_amp_resume_mismatch_fails_run() -> bool:
    complete = _run_with_fake_cli(
        "runner_amp", "amp",
        ['{"type":"system","subtype":"init","session_id":"T-other","tools":[]}'],
        {"prompt": "hi", "session_id": "T-requested"},
    )
    return _is_session_lost_failure(complete, "T-requested")


def test_qwen_resume_mismatch_fails_run() -> bool:
    complete = _run_with_fake_cli(
        "runner_qwen", "qwen",
        ['{"type":"system","subtype":"init","session_id":"other-sid","model":"m"}'],
        {"prompt": "hi", "session_id": "requested-sid"},
    )
    return _is_session_lost_failure(complete, "requested-sid")


def test_pi_resume_mismatch_fails_run() -> bool:
    from runs_dir import runs_root
    import runner_pi
    sid = "requested-pi-sid"
    sess_dir = runs_root() / "prior-run" / runner_pi.PI_SESSION_DIR_NAME / "--slug--"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"2026-07-09T00-00-00_{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    complete = _run_with_fake_cli(
        "runner_pi", "pi",
        ['{"type":"session","id":"other-pi-sid"}'],
        {"prompt": "hi", "session_id": sid},
    )
    return _is_session_lost_failure(complete, sid)


TESTS = [
    ("pi_auth_rule", test_pi_auth_rule),
    ("qwen_session_lost_rule", test_qwen_session_lost_rule),
    ("qwen_auth_rule", test_qwen_auth_rule),
    ("cursor_auth_rule", test_cursor_auth_rule),
    ("amp_rules", test_amp_rules),
    ("common_rules_apply_to_kimi_and_opencode", test_common_rules_apply_to_kimi_and_opencode),
    ("first_match_wins_provider_before_common", test_first_match_wins_provider_before_common),
    ("matched_line_and_message_fallback", test_matched_line_and_message_fallback),
    ("unknown_kind_fails_closed", test_unknown_kind_fails_closed),
    ("no_match_and_empty_return_none", test_no_match_and_empty_return_none),
    ("extract_stderr_error_named_error_first", test_extract_stderr_error_named_error_first),
    ("extract_stderr_error_error_prefix_then_last_line", test_extract_stderr_error_error_prefix_then_last_line),
    ("stderr_error_prefers_classification", test_stderr_error_prefers_classification),
    ("resume_session_mismatch", test_resume_session_mismatch),
    ("cursor_resume_mismatch_fails_run", test_cursor_resume_mismatch_fails_run),
    ("cursor_resume_same_sid_passes", test_cursor_resume_same_sid_passes),
    ("opencode_resume_mismatch_fails_run", test_opencode_resume_mismatch_fails_run),
    ("opencode_fork_new_sid_allowed", test_opencode_fork_new_sid_allowed),
    ("amp_resume_mismatch_fails_run", test_amp_resume_mismatch_fails_run),
    ("qwen_resume_mismatch_fails_run", test_qwen_resume_mismatch_fails_run),
    ("pi_resume_mismatch_fails_run", test_pi_resume_mismatch_fails_run),
]


def main() -> int:
    failures = []
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL: {name} (exception: {type(exc).__name__}: {exc})")
                failures.append(name)
                continue
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
