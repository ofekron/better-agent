"""delegate_task auto-routing cwd scoping is worktree-aware.

A caller cwd inside one worktree of a git repo must match sessions whose
cwd is in ANY sibling worktree (or subdirectory) of the same repo —
mirroring `session_matches_project` project-membership semantics (same
git common dir, resolved via git_repo_info's TTL cache). Non-git paths
keep exact normalized-equality matching, and no cwd filter (the '*'
opt-out resolved by the orchestrator to cwd=None) applies no cwd
constraint at all.

Run with:
    cd backend && .venv/bin/python scripts/test_session_search_cwd_worktree.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate state to a tempdir BEFORE importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-search-cwd-worktree-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import git_repo_info  # noqa: E402
import session_search  # noqa: E402
import session_store  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.email=t@t", "-c", "user.name=t",
            *args,
        ],
        check=True, capture_output=True,
    )


_REPOS = Path(tempfile.mkdtemp(prefix="bc-test-worktree-repos-"))

# Repo with two worktrees: repo-main (main worktree) + repo-dev (sibling).
_REPO_MAIN = _REPOS / "repo-main"
_REPO_DEV = _REPOS / "repo-dev"
_REPO_DEV_SUB = _REPO_DEV / "sub" / "dir"
# A separate, unrelated repo — must never fold into the first one.
_OTHER_REPO = _REPOS / "other-repo"
# Non-git directories — keep exact normalized-equality behavior.
_PLAIN = _REPOS / "plain"
_PLAIN_2 = _REPOS / "plain-2"


def _setup_repos() -> None:
    _REPO_MAIN.mkdir(parents=True)
    _git(_REPO_MAIN, "init", "-b", "main")
    _git(_REPO_MAIN, "commit", "--allow-empty", "-m", "init")
    _git(_REPO_MAIN, "worktree", "add", str(_REPO_DEV), "-b", "devbr")
    _REPO_DEV_SUB.mkdir(parents=True)
    _OTHER_REPO.mkdir(parents=True)
    _git(_OTHER_REPO, "init", "-b", "main")
    _git(_OTHER_REPO, "commit", "--allow-empty", "-m", "init")
    _PLAIN.mkdir(parents=True)
    _PLAIN_2.mkdir(parents=True)


def _reset_sessions() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with session_store._summary_index_lock:  # type: ignore[attr-defined]
        session_store._summary_index.clear()  # type: ignore[attr-defined]
        session_store._summary_sorted_id_cache.clear()  # type: ignore[attr-defined]
        session_store._summary_index_loaded = False  # type: ignore[attr-defined]
        session_store._summary_index_version = 0  # type: ignore[attr-defined]
        session_store._summary_order_version = 0  # type: ignore[attr-defined]
        session_store._summary_sorted_cache_version = -1  # type: ignore[attr-defined]
    git_repo_info.clear_caches()


def _write_session(*, sid: str, cwd: str) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": sid,
        "name": sid,
        "cwd": cwd,
        "messages": [{"role": "user", "content": "x"}],
        "updated_at": "2026-05-01T00:00:00",
        "archived": False,
        "user_initiated": True,
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(payload))


def _seed_all() -> None:
    _reset_sessions()
    _write_session(sid="main-sess", cwd=str(_REPO_MAIN))
    _write_session(sid="dev-sess", cwd=str(_REPO_DEV))
    _write_session(sid="sub-sess", cwd=str(_REPO_DEV_SUB))
    _write_session(sid="other-sess", cwd=str(_OTHER_REPO))
    _write_session(sid="plain-sess", cwd=str(_PLAIN))
    _write_session(sid="plain-2-sess", cwd=str(_PLAIN_2))


def _cwd_filter(cwd: str) -> dict:
    special, unsatisfiable = session_search._resolve_special_filters(cwd=cwd)
    if unsatisfiable:
        raise AssertionError(f"unexpected unsatisfiable: {unsatisfiable}")
    return special


def test_caller_cwd_matches_all_worktrees_of_the_repo() -> bool:
    """A caller in the main worktree scopes to sessions in ANY worktree
    (and subdirectories) of the same repo — never to unrelated repos or
    non-git dirs."""
    _seed_all()
    got = set(session_search._filtered_candidate_ids(_cwd_filter(str(_REPO_MAIN))))
    want = {"main-sess", "dev-sess", "sub-sess"}
    if got != want:
        print(f"{FAIL} main-worktree caller: got {sorted(got)} want {sorted(want)}")
        return False
    print(f"{PASS} caller cwd in main worktree matches sibling worktree + subdir sessions")
    return True


def test_caller_cwd_in_sibling_worktree_is_symmetric() -> bool:
    """The match is symmetric: a caller in the dev worktree (or a subdir
    of it) also sees the main-worktree session."""
    _seed_all()
    for caller in (str(_REPO_DEV), str(_REPO_DEV_SUB)):
        got = set(session_search._filtered_candidate_ids(_cwd_filter(caller)))
        want = {"main-sess", "dev-sess", "sub-sess"}
        if got != want:
            print(f"{FAIL} caller={caller}: got {sorted(got)} want {sorted(want)}")
            return False
    print(f"{PASS} sibling-worktree and subdir callers see the whole repo's sessions")
    return True


def test_validate_proposed_keeps_sibling_worktree_ids() -> bool:
    """The post-validation security boundary honors the same worktree-aware
    scope: sibling-worktree ids survive, unrelated-repo ids are dropped."""
    _seed_all()
    out = session_search.validate_proposed(
        ["dev-sess", "other-sess", "sub-sess", "plain-sess", "main-sess"],
        filters=_cwd_filter(str(_REPO_MAIN)),
    )
    if out != ["dev-sess", "sub-sess", "main-sess"]:
        print(f"{FAIL} validate_proposed worktree scope: got {out!r}")
        return False
    print(f"{PASS} validate_proposed keeps sibling-worktree ids, drops out-of-repo ids")
    return True


def test_non_git_cwd_keeps_exact_normalized_match() -> bool:
    """A non-git caller cwd matches only the exact directory (normalized),
    never other non-git dirs or repo sessions."""
    _seed_all()
    got = set(session_search._filtered_candidate_ids(_cwd_filter(str(_PLAIN))))
    if got != {"plain-sess"}:
        print(f"{FAIL} non-git exact: got {sorted(got)}")
        return False
    # Trailing slash normalizes to the same dir.
    got = set(session_search._filtered_candidate_ids(_cwd_filter(str(_PLAIN) + "/")))
    if got != {"plain-sess"}:
        print(f"{FAIL} non-git trailing slash: got {sorted(got)}")
        return False
    print(f"{PASS} non-git cwd keeps exact normalized-equality matching")
    return True


def test_no_cwd_filter_applies_no_scope() -> bool:
    """cwd=None (how the orchestrator resolves the '*' opt-out) produces no
    cwd filter, so every session stays in scope."""
    _seed_all()
    special, unsatisfiable = session_search._resolve_special_filters(cwd=None)
    if "cwd" in special or unsatisfiable:
        print(f"{FAIL} opt-out: special={special!r} unsatisfiable={unsatisfiable!r}")
        return False
    all_ids = {s["id"] for s in session_search._build_index()}
    want = {
        "main-sess", "dev-sess", "sub-sess",
        "other-sess", "plain-sess", "plain-2-sess",
    }
    if all_ids != want:
        print(f"{FAIL} opt-out index: got {sorted(all_ids)}")
        return False
    print(f"{PASS} no cwd filter ('*' opt-out) leaves all sessions in scope")
    return True


def main_run() -> int:
    _setup_repos()
    tests = [
        test_caller_cwd_matches_all_worktrees_of_the_repo,
        test_caller_cwd_in_sibling_worktree_is_symmetric,
        test_validate_proposed_keeps_sibling_worktree_ids,
        test_non_git_cwd_keeps_exact_normalized_match,
        test_no_cwd_filter_applies_no_scope,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} cwd-worktree scoping tests passed")
    shutil.rmtree(_REPOS, ignore_errors=True)
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
