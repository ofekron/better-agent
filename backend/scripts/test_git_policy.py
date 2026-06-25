"""Tests for git_policy — the inert-by-default enforcement foundation.

Locking is opt-in: only a session explicitly stamped git_policy="locked" gets
the git deny; absent (pre-extension) and "worker" sessions are unaffected. This
keeps the public core safe to ship before the git-control extension exists."""

import os
import sys
import tempfile

os.environ.setdefault("BETTER_AGENT_HOME", tempfile.mkdtemp(prefix="ba-git-policy-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import git_policy  # noqa: E402


def test_absent_session_no_deny_inert():
    assert git_policy.claude_disallowed_extra({}) == []
    assert git_policy.claude_disallowed_extra(None) == []
    assert git_policy.is_locked({}) is False
    assert git_policy.is_worker({}) is False


def test_locked_session_gets_claude_deny():
    extra = git_policy.claude_disallowed_extra({"git_policy": "locked"})
    assert "Bash(git:*)" in extra
    assert "Bash(gh:*)" in extra
    assert git_policy.is_locked({"git_policy": "locked"}) is True


def test_worker_session_no_deny():
    extra = git_policy.claude_disallowed_extra({"git_policy": "worker"})
    assert extra == []
    assert git_policy.is_worker({"git_policy": "worker"}) is True
    assert git_policy.is_locked({"git_policy": "worker"}) is False


def test_command_runs_git_matches_binaries_and_libs():
    for cmd in ["git status", "git commit -m x", "cd a && git push", "gh pr create",
                "python -c 'import pygit2'", "node -e 'require(\"isomorphic-git\")'",
                "sudo git pull", "ls; git log"]:
        assert git_policy.command_runs_git(cmd), cmd


def test_command_runs_git_no_false_positives():
    for cmd in ["ls -la", "echo digitize this", "cat file.txt", "npm run build",
                "echo engage", "grep widget a.txt"]:
        assert not git_policy.command_runs_git(cmd), cmd
