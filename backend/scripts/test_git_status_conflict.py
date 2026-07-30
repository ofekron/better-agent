from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-git-status-conflict-")

from file_browser import get_git_status, get_git_tree


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check,
    )


def _run() -> bool:
    repo = tempfile.mkdtemp(prefix="bc-git-status-conflict-repo-")
    try:
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test Author")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "commit.gpgsign", "false")
        readme = os.path.join(repo, "README.md")
        with open(readme, "w", encoding="utf-8") as handle:
            handle.write("main line\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "initial commit")

        _git(repo, "checkout", "-b", "feature-conflict")
        with open(readme, "w", encoding="utf-8") as handle:
            handle.write("feature line\n")
        _git(repo, "commit", "-am", "feature branch change")

        _git(repo, "checkout", "main")
        with open(readme, "w", encoding="utf-8") as handle:
            handle.write("main branch change\n")
        _git(repo, "commit", "-am", "main branch change")

        # Real merge conflict: exits non-zero, expected here, not a broken
        # command, so `check=False` instead of raising on the non-zero exit.
        merge = _git(repo, "merge", "feature-conflict", "--no-edit", check=False)
        if merge.returncode == 0:
            print(f"{FAIL} merge unexpectedly succeeded with no conflict: {merge.stdout!r}")
            return False

        porcelain = _git(repo, "status", "--porcelain").stdout
        if "UU README.md" not in porcelain:
            print(f"{FAIL} fixture did not produce the expected UU conflict: {porcelain!r}")
            return False

        status = get_git_status(repo)
        if not status.get("is_git"):
            print(f"{FAIL} get_git_status did not recognize the repo: {status!r}")
            return False
        if "README.md" not in status.get("conflicted", []):
            print(f"{FAIL} conflicted file not bucketed as conflicted: {status!r}")
            return False
        for key in ("modified", "added", "deleted", "untracked"):
            if "README.md" in status.get(key, []):
                print(f"{FAIL} conflicted file wrongly bucketed as {key}: {status!r}")
                return False

        tree = get_git_tree(repo, 20)
        if tree.get("dirty_count") != 1:
            print(f"{FAIL} dirty_count did not count the conflicted file: {tree!r}")
            return False
        if tree.get("conflicted_count") != 1:
            print(f"{FAIL} conflicted_count did not reflect the conflicted file: {tree!r}")
            return False

        print(f"{PASS} get_git_status/get_git_tree surface an unmerged conflict distinctly, not dropped")
        return True
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    try:
        ok = _run()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
