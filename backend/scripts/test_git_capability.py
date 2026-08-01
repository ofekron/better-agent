"""Unit + real-git coverage for git_capability.py — the subprocess-driven git
tool exposed to agents. This module is security-critical: it spawns `git`
with partially untrusted input (paths, refs, remotes, messages), so the bulk
of the coverage is the injection-prevention validators and the execution-time
security gates (path escape, ref injection, push-must-be-https, clean-filter
RCE rejection, cwd/repo binding).

Two layers:
- Pure validators (_paths, _branch): no git, pure input rejection.
- Execution paths (_run, _repo, _execute, execute): driven against a REAL
  throwaway git repo in tmp_path (deterministic, fast) with the
  session_manager.get_ref seam mocked so no real session store is needed.

Run:
    cd backend && .venv/bin/python -m pytest scripts/test_git_capability.py \
        --cov=git_capability --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import git_capability  # noqa: E402


# ---------------------------------------------------------------------------
# tmp git repo fixture
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    """Raw git against the fixture repo (full env) — bypasses git_capability's
    hardened env so we can set up identity/commits the module then operates on."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"setup git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _session(cwd: Path) -> dict:
    return {"cwd": str(cwd)}


@pytest.fixture
def bound_session(repo: Path):
    """Patch session_manager.get_ref to bind actor_session_id -> repo cwd."""
    with mock.patch.object(git_capability.session_manager, "get_ref") as m:
        m.return_value = _session(repo)
        yield m


# ===========================================================================
# _paths — pure path-injection prevention
# ===========================================================================
class TestPaths:
    def test_valid_relative_path_returned(self, tmp_path):
        assert git_capability._paths(tmp_path, ["src/main.py"]) == ["src/main.py"]

    def test_valid_multiple_returned_in_order(self, tmp_path):
        assert git_capability._paths(tmp_path, ["a", "b/c"]) == ["a", "b/c"]

    def test_valid_subdir_path(self, repo):
        (repo / "sub").mkdir()
        assert git_capability._paths(repo, ["sub/x"]) == ["sub/x"]

    def test_absolute_path_rejected_403(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, ["/etc/passwd"])
        assert exc.value.status_code == 403

    def test_absolute_path_inside_repo_still_rejected_403(self, tmp_path):
        # An absolute path that resolves INSIDE the repo must still be refused:
        # callers must use repo-relative paths. Only the is_absolute() guard
        # catches this (is_relative_to would pass), so this locks that check.
        (tmp_path / "sub").mkdir()
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, [str(tmp_path / "sub" / "x")])
        assert exc.value.status_code == 403

    def test_dotdot_escape_rejected_403(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, ["../outside"])
        assert exc.value.status_code == 403

    def test_empty_string_rejected_422(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, [""])
        assert exc.value.status_code == 422

    def test_non_string_rejected_422(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, [123])  # type: ignore[list-item]
        assert exc.value.status_code == 422

    def test_leading_colon_rejected_422(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, [":path"])
        assert exc.value.status_code == 422

    def test_embedded_null_byte_rejected_422(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            git_capability._paths(tmp_path, ["a\x00b"])
        assert exc.value.status_code == 422

    def test_first_bad_path_aborts_whole_batch(self, tmp_path):
        # A valid path followed by a bad one still raises; nothing is returned.
        with pytest.raises(HTTPException):
            git_capability._paths(tmp_path, ["ok", "../escape"])


# ===========================================================================
# _branch — pure git-ref injection prevention
# ===========================================================================
class TestBranch:
    @pytest.mark.parametrize("name", ["main", "feature/add-thing", "release-1.0", "a/b/c"])
    def test_valid_branch_returned(self, name):
        assert git_capability._branch(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "..",            # double-dot (ref range)
            "a..b",          # embedded range
            "a~1",           # tilde
            "a^",            # caret
            "a:b",           # colon
            "a?",            # glob question
            "a*b",           # glob star
            "a[b",           # bracket
            "a\\b",          # backslash
            "a b",           # whitespace
            "-branch",       # leading dash (option injection)
            "branch.",       # trailing dot
            "branch/",       # trailing slash
            "a@{b",          # reflog shorthand
            "",              # empty
        ],
    )
    def test_invalid_branch_rejected_422(self, name):
        with pytest.raises(HTTPException) as exc:
            git_capability._branch(name)
        assert exc.value.status_code == 422


# ===========================================================================
# _run — subprocess wrapper result shaping
# ===========================================================================
class TestRun:
    def test_ok_command_returns_ok_result(self, repo):
        result = git_capability._run(repo, ["rev-parse", "--show-toplevel"])
        assert result["result"] == "ok"
        assert Path(result["output"]).resolve() == repo.resolve()

    def test_failing_command_returns_error_with_returncode(self, repo):
        result = git_capability._run(repo, ["show", "nope-no-such-object"])
        assert result["result"] == "error"
        assert result["returncode"] != 0


# ===========================================================================
# _repo — session/cwd binding + repo discovery
# ===========================================================================
class TestRepo:
    def test_unknown_actor_session_404(self):
        with mock.patch.object(git_capability.session_manager, "get_ref", return_value=None):
            with pytest.raises(HTTPException) as exc:
                git_capability._repo({"actor_session_id": "ghost", "cwd": "."})
        assert exc.value.status_code == 404

    def test_cwd_mismatch_403(self, repo, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        with mock.patch.object(
            git_capability.session_manager, "get_ref", return_value=_session(repo),
        ):
            with pytest.raises(HTTPException) as exc:
                git_capability._repo({"actor_session_id": "s", "cwd": str(other)})
        assert exc.value.status_code == 403

    def test_cwd_not_a_directory_400(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("x")
        with mock.patch.object(
            git_capability.session_manager, "get_ref", return_value=_session(f),
        ):
            with pytest.raises(HTTPException) as exc:
                git_capability._repo({"actor_session_id": "s", "cwd": str(f)})
        assert exc.value.status_code == 400

    def test_cwd_not_a_git_repo_400(self, tmp_path):
        bare = tmp_path / "notgit"
        bare.mkdir()
        with mock.patch.object(
            git_capability.session_manager, "get_ref", return_value=_session(bare),
        ):
            with pytest.raises(HTTPException) as exc:
                git_capability._repo({"actor_session_id": "s", "cwd": str(bare)})
        assert exc.value.status_code == 400

    def test_valid_repo_returns_resolved_root(self, repo, bound_session):
        root = git_capability._repo({"actor_session_id": "s", "cwd": str(repo)})
        assert root.resolve() == repo.resolve()
        bound_session.assert_called_once_with("s")


# ===========================================================================
# _execute — action dispatch + execution-time security gates
# ===========================================================================
class TestExecute:
    def _payload(self, repo: Path, **extra) -> dict:
        base = {"actor_session_id": "s", "cwd": str(repo)}
        base.update(extra)
        return base

    def test_status(self, repo, bound_session):
        assert git_capability._execute("status", self._payload(repo))["result"] == "ok"

    def test_diff_unstaged(self, repo, bound_session):
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        assert git_capability._execute(
            "diff", self._payload(repo, staged=False),
        )["result"] == "ok"

    def test_diff_staged(self, repo, bound_session):
        (repo / "new.txt").write_text("n", encoding="utf-8")
        _git(repo, "add", "new.txt")
        assert git_capability._execute(
            "diff", self._payload(repo, staged=True),
        )["result"] == "ok"

    def test_log_with_limit(self, repo, bound_session):
        result = git_capability._execute("log", self._payload(repo, limit=5))
        assert result["result"] == "ok"
        assert "init" in result["output"]

    def test_add_valid_paths(self, repo, bound_session):
        (repo / "a.txt").write_text("a", encoding="utf-8")
        assert git_capability._execute(
            "add", self._payload(repo, paths=["a.txt"]),
        )["result"] == "ok"

    def test_add_path_escape_rejected_403(self, repo, bound_session):
        with pytest.raises(HTTPException) as exc:
            git_capability._execute("add", self._payload(repo, paths=["../escape"]))
        assert exc.value.status_code == 403

    def test_add_aborts_when_clean_filter_present(self, repo, bound_session):
        # An executable clean/process filter is an RCE vector; add must refuse.
        _git(repo, "config", "filter.demo.clean", "cat")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        with pytest.raises(HTTPException) as exc:
            git_capability._execute("add", self._payload(repo, paths=["a.txt"]))
        assert exc.value.status_code == 403

    def test_commit(self, repo, bound_session):
        (repo / "c.txt").write_text("c", encoding="utf-8")
        git_capability._execute("add", self._payload(repo, paths=["c.txt"]))
        result = git_capability._execute(
            "commit", self._payload(repo, message="add c"),
        )
        assert result["result"] == "ok"

    def test_branch_create(self, repo, bound_session):
        assert git_capability._execute(
            "branch", self._payload(repo, name="feat", create=True),
        )["result"] == "ok"

    def test_branch_list_existing(self, repo, bound_session):
        _git(repo, "branch", "feat")
        result = git_capability._execute(
            "branch", self._payload(repo, name="feat", create=False),
        )
        assert result["result"] == "ok"
        assert "feat" in result["output"]

    def test_push_non_https_remote_rejected_403(self, repo, bound_session):
        _git(repo, "remote", "add", "origin", "git@example.com:r.git")
        with pytest.raises(HTTPException) as exc:
            git_capability._execute(
                "push", self._payload(repo, remote="origin", ref="main"),
            )
        assert exc.value.status_code == 403

    def test_push_https_remote_passes_scheme_gate(self, repo, bound_session):
        # https remote clears the scheme gate; the push itself then fails
        # (no real upstream) and returns an error result rather than raising.
        _git(repo, "remote", "add", "origin", "https://example.com/r.git")
        result = git_capability._execute(
            "push", self._payload(repo, remote="origin", ref="main"),
        )
        assert result["result"] == "error"

    def test_push_missing_remote_rejected_403(self, repo, bound_session):
        # get-url --push fails for an unknown remote -> treated as forbidden.
        with pytest.raises(HTTPException) as exc:
            git_capability._execute(
                "push", self._payload(repo, remote="nope", ref="main"),
            )
        assert exc.value.status_code == 403

    def test_push_no_ref_omits_ref_arg(self, repo, bound_session):
        # https remote, empty ref -> push with just the remote (fails at the
        # network layer, not the gate). Confirms the ref branch is skipped.
        _git(repo, "remote", "add", "origin", "https://example.com/r.git")
        result = git_capability._execute(
            "push", self._payload(repo, remote="origin", ref=""),
        )
        assert result["result"] == "error"

    def test_unknown_action_404(self, repo, bound_session):
        with pytest.raises(HTTPException) as exc:
            git_capability._execute("explode", self._payload(repo))
        assert exc.value.status_code == 404


# ===========================================================================
# execute — async to_thread wrapper
# ===========================================================================
class TestExecuteAsync:
    def test_execute_delegates_to_execute_sync(self, repo, bound_session):
        payload = {"actor_session_id": "s", "cwd": str(repo)}
        result = asyncio.run(git_capability.execute("status", payload))
        assert result["result"] == "ok"
