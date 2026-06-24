"""Regression test for private Better Agent state-directory permissions.

Run with:
    cd backend && .venv/bin/python scripts/test_ba_home_permissions.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run_isolated_posix() -> bool:
    script = r"""
import os
import stat
import sys
import tempfile

os.umask(0o022)
home = tempfile.mkdtemp(prefix="bc-test-home-perms-")
os.environ["BETTER_AGENT_HOME"] = home
os.environ["BETTER_CLAUDE_HOME"] = home
sys.path.insert(0, sys.argv[1])

from paths import ba_home

root = ba_home()
subdir = root / "sessions"
subdir.mkdir()
file_path = root / "config.json"
file_path.write_text("{}", encoding="utf-8")

checks = [
    stat.S_IMODE(root.stat().st_mode) == 0o700,
    stat.S_IMODE(subdir.stat().st_mode) == 0o700,
    stat.S_IMODE(file_path.stat().st_mode) == 0o600,
]
print(" ".join(oct(stat.S_IMODE(p.stat().st_mode)) for p in [root, subdir, file_path]))
sys.exit(0 if all(checks) else 1)
"""
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(backend)],
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def _run_home_compat_posix() -> bool:
    script = r"""
import os
import sys
import tempfile
from pathlib import Path

backend = sys.argv[1]
sys.path.insert(0, backend)

def fresh_paths():
    sys.modules.pop("paths", None)
    from paths import ba_home
    return ba_home

def reset_env(home):
    os.environ["HOME"] = str(home)
    os.environ.pop("BETTER_AGENT_HOME", None)
    os.environ.pop("BETTER_CLAUDE_HOME", None)

base = Path(tempfile.mkdtemp(prefix="bc-test-home-compat-"))

home = base / "default-home"
home.mkdir()
reset_env(home)
ba_home = fresh_paths()
root = ba_home()
alias = home / ".better-agent"
legacy = home / ".better-claude"
assert root == legacy, root
assert alias.is_symlink(), alias
assert alias.resolve() == legacy.resolve(), alias.resolve()

home = base / "new-only-home"
home.mkdir()
(home / ".better-agent").mkdir()
reset_env(home)
ba_home = fresh_paths()
assert ba_home() == home / ".better-agent"

home = base / "env-home"
home.mkdir()
reset_env(home)
new_env = base / "env-new"
old_env = base / "env-old"
os.environ["BETTER_AGENT_HOME"] = str(new_env)
os.environ["BETTER_CLAUDE_HOME"] = str(old_env)
ba_home = fresh_paths()
assert ba_home() == new_env

home = base / "legacy-env-home"
home.mkdir()
reset_env(home)
os.environ["BETTER_CLAUDE_HOME"] = str(old_env)
ba_home = fresh_paths()
assert ba_home() == old_env

home = base / "relative-env-home"
home.mkdir()
reset_env(home)
os.environ["BETTER_AGENT_HOME"] = "relative/path"
ba_home = fresh_paths()
try:
    ba_home()
except ValueError as exc:
    assert "BETTER_AGENT_HOME" in str(exc)
else:
    raise AssertionError("relative BETTER_AGENT_HOME was accepted")
"""
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(backend)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout.strip())
        print(result.stderr.strip())
    return result.returncode == 0


def _windows_acl_text(path: Path) -> str:
    result = subprocess.run(
        ["icacls", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _run_windows() -> bool:
    home = Path(tempfile.mkdtemp(prefix="bc-test-home-perms-"))
    old_home = os.environ.get("BETTER_CLAUDE_HOME")
    old_agent_home = os.environ.get("BETTER_AGENT_HOME")
    try:
        os.environ["BETTER_AGENT_HOME"] = str(home)
        os.environ["BETTER_CLAUDE_HOME"] = str(home)
        backend = Path(__file__).resolve().parents[1]
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from paths import ba_home

        root = ba_home()
        acl = _windows_acl_text(root)
        forbidden = ["Everyone", "BUILTIN\\Users", "S-1-1-0", "S-1-5-32-545"]
        return all(item not in acl for item in forbidden)
    finally:
        if old_home is None:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        else:
            os.environ["BETTER_CLAUDE_HOME"] = old_home
        if old_agent_home is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = old_agent_home
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    ok = _run_windows() if os.name == "nt" else _run_isolated_posix()
    if os.name != "nt":
        ok = _run_home_compat_posix() and ok
    print(f"{PASS if ok else FAIL} ba_home uses private permissions and home aliases")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
