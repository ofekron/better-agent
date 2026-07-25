"""Locks `project_store` git-remote discovery + refresh against real repos:

1. A repo whose only remote is NOT named `origin` still resolves.
2. `origin` wins when several remotes exist.
3. A directory that is not a git repo resolves to "no remote" (not unknown).
4. `backfill_git_remotes` OVERWRITES a stale stored remote.
5. `backfill_git_remotes` CLEARS the stored remote when the repo lost it.
6. `backfill_git_remotes` leaves a project whose path is gone untouched, so an
   offline volume never erases a known remote.

Run with:
    cd backend && .venv/bin/python scripts/test_project_git_remote_refresh.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-gitremote-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    return repo


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="bc-gitremote-"))
    try:
        # 1. remote not named origin
        named = make_repo(work, "named")
        git(named, "remote", "add", "github", "git@github.com:ofekron/better-agent.git")
        check(
            "non-origin remote resolves",
            project_store.ensure_git_remote(str(named)) == "git@github.com:ofekron/better-agent.git",
        )

        # 2. origin preferred
        multi = make_repo(work, "multi")
        git(multi, "remote", "add", "upstream", "git@github.com:acme/upstream.git")
        git(multi, "remote", "add", "origin", "git@github.com:acme/origin.git")
        check(
            "origin preferred over other remotes",
            project_store.ensure_git_remote(str(multi)) == "git@github.com:acme/origin.git",
        )

        # 3. plain directory -> resolved, no remote
        plain = work / "plain"
        plain.mkdir()
        check("non-repo directory resolves to no remote", project_store.probe_git_remote(str(plain)) == (True, None))

        # missing path -> unresolved
        check(
            "missing path is unresolved, not 'no remote'",
            project_store.probe_git_remote(str(work / "gone")) == (False, None),
        )

        # 4/5/6. backfill over a registry holding a stale value, a lost remote,
        # and a vanished checkout.
        gone = make_repo(work, "gone")
        # add_project resolves symlinks, so track the stored paths it returns.
        named_key = project_store.add_project(path=str(named), name="named")["path"]
        project_store.add_project(path=str(multi), name="multi")
        plain_key = project_store.add_project(path=str(plain), name="plain")["path"]
        gone_key = project_store.add_project(path=str(gone), name="gone")["path"]

        stale = {
            named_key: "git@gitlab.com:stale/stale.git",
            plain_key: "git@gitlab.com:was-a-repo/x.git",
            gone_key: "git@github.com:acme/gone.git",
        }
        registry = project_store._read_file()
        for record in registry:
            if record.get("path") in stale:
                record["git_remote"] = stale[record["path"]]
        project_store._write_file(registry)
        shutil.rmtree(gone)

        project_store.backfill_git_remotes()
        by_path = {p.get("path"): p for p in project_store.list_projects()}

        check(
            "stale remote is corrected from the checkout",
            by_path[named_key].get("git_remote") == "git@github.com:ofekron/better-agent.git",
        )
        check(
            "remote is cleared when the checkout has none",
            by_path[plain_key].get("git_remote") is None,
        )
        check(
            "vanished checkout keeps its known remote",
            by_path[gone_key].get("git_remote") == "git@github.com:acme/gone.git",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
