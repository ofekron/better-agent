from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-git-tree-")

from file_browser import get_git_tree


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _run() -> bool:
    repo = tempfile.mkdtemp(prefix="bc-git-tree-repo-")
    try:
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test Author")
        _git(repo, "config", "user.email", "test@example.com")
        first = os.path.join(repo, "first.txt")
        with open(first, "w", encoding="utf-8") as handle:
            handle.write("first\n")
        _git(repo, "add", "first.txt")
        _git(repo, "commit", "-m", "first commit")
        first_hash = _git(repo, "rev-parse", "HEAD")

        _git(repo, "checkout", "-b", "feature")
        with open(first, "a", encoding="utf-8") as handle:
            handle.write("feature\n")
        _git(repo, "commit", "-am", "feature commit")
        feature_hash = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "main")

        tree = get_git_tree(repo, 20)
        commits = tree.get("commits", [])
        by_hash = {commit["hash"]: commit for commit in commits}
        if tree.get("branch") != "main" or tree.get("dirty_count") != 0:
            print(f"{FAIL} branch/status metadata is wrong: {tree!r}")
            return False
        if first_hash not in by_hash or feature_hash not in by_hash:
            print(f"{FAIL} all-branch history is incomplete: {commits!r}")
            return False
        if by_hash[feature_hash]["parents"] != [first_hash]:
            print(f"{FAIL} parent relationship is wrong: {by_hash[feature_hash]!r}")
            return False
        if "feature" not in by_hash[feature_hash]["refs"]:
            print(f"{FAIL} branch decoration is missing: {by_hash[feature_hash]!r}")
            return False

        with open(os.path.join(repo, "dirty.txt"), "w", encoding="utf-8") as handle:
            handle.write("dirty\n")
        dirty_tree = get_git_tree(repo, 1)
        if dirty_tree.get("dirty_count") != 1 or len(dirty_tree.get("commits", [])) != 1:
            print(f"{FAIL} dirty count or limit is wrong: {dirty_tree!r}")
            return False

        print(f"{PASS} git tree returns bounded topological history and repository metadata")
        return True
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    try:
        ok = _run()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
