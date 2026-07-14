"""Worktree-aware project grouping + session matching.

Locks the behavior added for "worktrees level below project when there
is more than one worktree":

1. `list_projects_grouped` collapses every worktree of one repo into a
   single project record whose `path` is the main worktree and whose
   `worktrees[]` lists each existing checkout.
2. `session_matches_project` matches a session to a project by git
   COMMON DIR, so a session in any worktree (or subdirectory) of the
   repo belongs to the project.
3. A NESTED repo (a separate repo checked out inside a worktree) does
   NOT match the parent project — it resolves to a different common dir.
4. Non-git projects keep exact-path matching.

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_project_worktree_grouping.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worktree-group-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import git_repo_info  # noqa: E402
import project_store  # noqa: E402
from session_manager import session_matches_project  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _git(args: list[str], cwd: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=env)


def _build_repo() -> tuple[str, str, str]:
    """Build a repo with a main worktree + one linked worktree, plus a
    nested separate repo inside the main worktree. Returns
    (main_path, worktree_path, nested_repo_path). Paths are realpath'd
    so they match what git/project_store resolve (macOS firlinks map
    /var -> /private/var)."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="wt-main-"))
    _git(["init", "-q", "-b", "main", root], root)
    open(os.path.join(root, "README.md"), "w").write("hi\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "init"], root)
    wt = os.path.realpath(tempfile.mkdtemp(prefix="wt-linked-"))
    _git(["worktree", "add", "-q", "-b", "feature", wt], root)
    # Nested separate repo inside the main worktree.
    nested = os.path.join(root, "nested")
    os.makedirs(nested)
    _git(["init", "-q", "-b", "main", nested], nested)
    open(os.path.join(nested, "x.txt"), "w").write("x\n")
    _git(["add", "."], nested)
    _git(["commit", "-q", "-m", "nested init"], nested)
    return root, wt, nested


def test_grouping_collapses_worktrees() -> None:
    root, wt, _nested = _build_repo()
    git_repo_info.clear_caches()
    try:
        project_store.add_project(root, name="myrepo")
        project_store.add_project(wt, name="myrepo-linked")
        groups = project_store.list_projects_grouped()
        repos = [g for g in groups if g["path"] == root]
        assert len(repos) == 1, f"expected 1 group for repo, got {len(repos)}"
        g = repos[0]
        wt_paths = {w["path"] for w in g["worktrees"]}
        assert root in wt_paths, f"main worktree missing from {wt_paths}"
        assert wt in wt_paths, f"linked worktree missing from {wt_paths}"
        mains = [w for w in g["worktrees"] if w["is_main"]]
        assert len(mains) == 1 and mains[0]["path"] == root, (
            f"expected exactly the main worktree as main, got {mains}"
        )
        print(f"{PASS} grouping_collapses_worktrees")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(wt, ignore_errors=True)


def test_matching_sibling_and_subdir_match() -> None:
    root, wt, _nested = _build_repo()
    git_repo_info.clear_caches()
    try:
        assert session_matches_project({"cwd": wt}, root), "sibling worktree should match"
        # A subdir of the main worktree resolves to the same common dir.
        os.makedirs(os.path.join(root, "sub"))
        assert session_matches_project({"cwd": os.path.join(root, "sub")}, root), (
            "subdir of worktree should match"
        )
        print(f"{PASS} matching_sibling_and_subdir_match")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(wt, ignore_errors=True)


def test_nested_repo_does_not_match_parent() -> None:
    root, _wt, nested = _build_repo()
    git_repo_info.clear_caches()
    try:
        matched = session_matches_project({"cwd": nested}, root)
        assert matched is False, (
            f"nested repo must NOT match parent project (got {matched})"
        )
        # The nested repo matches its OWN project.
        assert session_matches_project({"cwd": nested}, nested), (
            "nested repo should match its own project"
        )
        print(f"{PASS} nested_repo_does_not_match_parent")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_non_git_exact_match() -> None:
    d = os.path.realpath(tempfile.mkdtemp(prefix="wt-nogit-"))
    git_repo_info.clear_caches()
    try:
        # Non-git: common dir is None -> exact path match only.
        assert session_matches_project({"cwd": d}, d), "exact non-git match"
        assert session_matches_project({"cwd": d + "/sub"}, d) is False, (
            "non-git subdir must not match (exact only)"
        )
        assert session_matches_project({"cwd": "/elsewhere"}, d) is False, (
            "non-git mismatch must not match"
        )
        print(f"{PASS} non_git_exact_match")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_all_projects_flag_visible_everywhere() -> None:
    root, _wt, _nested = _build_repo()
    git_repo_info.clear_caches()
    try:
        assert session_matches_project({"cwd": "/unrelated", "all_projects": True}, root), (
            "all_projects session must be visible in every project"
        )
        print(f"{PASS} all_projects_flag_visible_everywhere")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_common_dir_expiry_does_not_compose_ttls() -> None:
    git_repo_info.clear_caches()
    now = 10.0
    with (
        patch.object(git_repo_info, "_now", side_effect=lambda: now),
        patch.object(git_repo_info, "_run_git", return_value=None),
    ):
        _identity, first_expiry, _generation = (
            git_repo_info.repo_common_dir_with_expiry("/tmp/expiry-test")
        )
        now = 69.0
        _identity, second_expiry, _generation = (
            git_repo_info.repo_common_dir_with_expiry("/tmp/expiry-test")
        )
    assert first_expiry == second_expiry == 70.0
    print(f"{PASS} common_dir_expiry_does_not_compose_ttls")


def test_clear_during_common_dir_lookup_discards_stale_result() -> None:
    git_repo_info.clear_caches()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_git(_args, _cwd):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            return "/tmp/stale-git-dir\n"
        return "/tmp/fresh-git-dir\n"

    result = []
    with patch.object(git_repo_info, "_run_git", blocked_git):
        thread = threading.Thread(
            target=lambda: result.append(
                git_repo_info.repo_common_dir_with_expiry("/tmp/race-test")
            )
        )
        thread.start()
        assert entered.wait(5)
        git_repo_info.clear_caches()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
    assert calls == 2
    assert result[0][0] == os.path.realpath("/tmp/fresh-git-dir")
    assert result[0][2] == git_repo_info.cache_generation_snapshot()
    print(f"{PASS} clear_during_common_dir_lookup_discards_stale_result")


def test_clear_during_worktree_lookup_discards_stale_result() -> None:
    git_repo_info.clear_caches()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_git(_args, _cwd):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            return "worktree /tmp/stale-worktree\n\n"
        return "worktree /tmp/fresh-worktree\n\n"

    result = []
    with patch.object(git_repo_info, "_run_git", blocked_git):
        thread = threading.Thread(
            target=lambda: result.append(
                git_repo_info.worktree_entries("/tmp/worktree-race")
            )
        )
        thread.start()
        assert entered.wait(5)
        git_repo_info.clear_caches()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
    assert calls == 2
    assert result[0][0]["path"] == "/tmp/fresh-worktree"
    print(f"{PASS} clear_during_worktree_lookup_discards_stale_result")


def main() -> int:
    try:
        test_grouping_collapses_worktrees()
        test_matching_sibling_and_subdir_match()
        test_nested_repo_does_not_match_parent()
        test_non_git_exact_match()
        test_all_projects_flag_visible_everywhere()
        test_common_dir_expiry_does_not_compose_ttls()
        test_clear_during_common_dir_lookup_discards_stale_result()
        test_clear_during_worktree_lookup_discards_stale_result()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
