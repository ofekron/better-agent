"""Single source of truth for the Better Agent state directory.

All persistence modules (`session_store`, `worker_store`,
`pending_approvals`, `project_store`, `provider_claude`,
`trace_collector`, `rearranger_state`, `orchestrator`'s internal
token path, etc.) MUST resolve their on-disk locations through
`ba_home()` rather than hardcoding `Path.home() / ".better-claude"`.

Why: tests need to run against an isolated tempdir so they can't
clobber the developer's real sessions/workers/etc. Setting
`BETTER_AGENT_HOME=/tmp/whatever` before importing the backend
modules redirects every store to the tempdir. `BETTER_CLAUDE_HOME`
remains supported as the legacy fallback. Without either override,
production behavior keeps using `~/.better-claude` and creates
`~/.better-agent` as a local alias when it can do so safely.

DO NOT cache the resolved Path at module import time — tests override
the env var inside the test process and modules imported before the
override would otherwise miss it. Always call `ba_home()` per access,
or compute paths on demand inside functions rather than at module
scope.
"""

import os
import subprocess
from pathlib import Path

try:  # POSIX only; absent on Windows.
    import pwd as _pwd
except ImportError:  # pragma: no cover - non-POSIX
    _pwd = None


_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_UMASK = 0o077
_SECURED_ROOTS: set[str] = set()
_WINDOWS_CURRENT_USER_SID: str | None = None
_PRIMARY_HOME_ENV = "BETTER_AGENT_HOME"
_LEGACY_HOME_ENV = "BETTER_CLAUDE_HOME"
_DEFAULT_STATE_DIR = ".better-claude"
_DEFAULT_ALIAS_DIR = ".better-agent"

# When set (by _test_home / the test conftest), ba_home() refuses any state
# root at or under the OS user home, so a leaked/inherited real
# BETTER_AGENT_HOME can never route a test onto the production home. Env var
# (not a Python flag) so spawned subprocesses inherit it.
_TEST_MODE_ENV = "BETTER_AGENT_TEST_MODE"
_USER_HOME: Path | None = None


def user_home() -> Path:
    """The OS user's home dir, derived from the passwd db (NOT $HOME).

    `$HOME`/`Path.home()` is env-spoofable, so it cannot be the reference
    frame for a guard that exists to stop tests deleting the real home.
    `pwd.getpwuid` reads the actual account record. Cached per process —
    a uid's passwd entry does not change.
    """
    global _USER_HOME
    if _USER_HOME is None:
        if _pwd is not None:
            _USER_HOME = Path(_pwd.getpwuid(os.getuid()).pw_dir)
        else:  # pragma: no cover - non-POSIX fallback
            _USER_HOME = Path.home()
    return _USER_HOME


def is_test_mode() -> bool:
    """True inside a test process (conftest / _test_home set the sentinel)."""
    return os.environ.get(_TEST_MODE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def prod_state_roots() -> list[Path]:
    """The production state dirs a test must never touch: the default home and
    its alias, anchored on the pwd-derived (not `$HOME`) user dir."""
    base = user_home()
    return [base / _DEFAULT_STATE_DIR, base / _DEFAULT_ALIAS_DIR]


def assert_state_root_safe(root: Path) -> None:
    """Fail-closed guard: in test mode, reject any state root at or under a
    production state dir (`~/.better-claude` / `~/.better-agent`). Covers the
    production default, the alias, AND an explicitly inherited real
    `BETTER_AGENT_HOME` (the branch `Path.home()`-based guards miss). Resolves
    to defeat `..` and symlink tricks. Anchored on `pwd`, not `$HOME`."""
    if not is_test_mode():
        return
    resolved = root.expanduser().resolve()
    for prod in prod_state_roots():
        prod_r = prod.resolve()
        if resolved == prod_r or prod_r in resolved.parents:
            raise RuntimeError(
                f"{_TEST_MODE_ENV} is set; refusing state root under the "
                f"production home ({prod_r}). Point BETTER_AGENT_HOME at a tempdir."
            )


def _install_private_umask() -> None:
    if os.name == "nt":
        return
    old = os.umask(_PRIVATE_FILE_UMASK)
    tighter = old | _PRIVATE_FILE_UMASK
    if tighter != _PRIVATE_FILE_UMASK:
        os.umask(tighter)


def _windows_current_user_sid() -> str:
    global _WINDOWS_CURRENT_USER_SID
    if _WINDOWS_CURRENT_USER_SID is not None:
        return _WINDOWS_CURRENT_USER_SID
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sid = result.stdout.strip()
    if not sid:
        raise RuntimeError("failed to resolve current Windows user SID")
    _WINDOWS_CURRENT_USER_SID = sid
    return _WINDOWS_CURRENT_USER_SID


def _make_private(path: Path) -> None:
    if os.name == "nt":
        sid = _windows_current_user_sid()
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "/grant:r",
                "*S-1-5-32-544:(OI)(CI)F",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "/remove:g",
                "*S-1-1-0",
                "*S-1-5-32-545",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    path.chmod(_PRIVATE_DIR_MODE)


_install_private_umask()


def _env_home(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {raw!r}")
    return root


def _ensure_default_alias(root: Path) -> None:
    if os.name == "nt":
        return
    alias = Path.home() / _DEFAULT_ALIAS_DIR
    if alias.exists() or alias.is_symlink():
        return
    alias.symlink_to(root)


def _default_home() -> Path:
    home = Path.home()
    legacy = home / _DEFAULT_STATE_DIR
    alias = home / _DEFAULT_ALIAS_DIR
    if alias.exists() and not legacy.exists():
        return alias
    return legacy


def ba_home() -> Path:
    """Resolve the Better Agent state root.

    Honors `BETTER_AGENT_HOME` if set, then `BETTER_CLAUDE_HOME` as
    legacy fallback. Env values must be absolute. Without env config,
    defaults to `~/.better-claude`; when that path is used, creates
    `~/.better-agent` as a local alias if possible.
    """
    configured = _env_home(_PRIMARY_HOME_ENV) or _env_home(_LEGACY_HOME_ENV)
    root = configured or _default_home()
    assert_state_root_safe(root)
    root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if configured is None and root.name == _DEFAULT_STATE_DIR:
        _ensure_default_alias(root)
    secured_key = str(root.resolve())
    if secured_key not in _SECURED_ROOTS:
        _make_private(root)
        _SECURED_ROOTS.add(secured_key)
    return root


# Alias to satisfy requirements extension and global rules.
bc_home = ba_home



def encode_cwd(cwd: str) -> str:
    """Normalize a cwd into a filesystem-safe token.

    Matches claude CLI's own ~/.claude/projects/ encoding: replace `/`,
    `\\`, `:`, AND `_` with `-`. The `_`→`-` rule is non-obvious but
    verified — paths containing underscores resolve to the same encoded
    name in both stores, so a stale code path that recomputes from cwd
    hits the right file. On Windows the `:`→`-` rule is required so the
    drive letter encodes the same way Claude CLI writes it: `C:\\foo`
    becomes `C--foo` (NOT `C:-foo`), matching `~/.claude/projects/C--foo/`.
    Without this, the tailer opens a path that doesn't exist and
    ingestion silently fails on Windows.
    """
    resolved = Path(cwd).expanduser().resolve().as_posix()
    return (
        resolved.replace("/", "-").replace("\\", "-").replace(":", "-").replace("_", "-")
        or "root"
    )


def claude_projects_root_for_session(node: dict) -> Path:
    """Resolve the claude projects directory for a session's provider.

    Providers can set `CLAUDE_CONFIG_DIR` (e.g. ~/.claude-zai), which
    changes where the claude CLI writes its JSONL files. Order of
    precedence: session's `provider_id` → its provider record's
    `config_dir` → the `CLAUDE_CONFIG_DIR` env var → default
    `~/.claude/projects`.

    Import is local to avoid a paths→config_store cycle at module
    init time (config_store imports paths).
    """
    import config_store
    provider_id = node.get("provider_id")
    if provider_id:
        rec = config_store.get_provider(provider_id)
        if rec:
            cfg_dir = (rec.get("config_dir") or "").strip()
            if cfg_dir:
                return Path(os.path.expanduser(os.path.expandvars(cfg_dir))) / "projects"
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if env_dir:
        return Path(os.path.expanduser(os.path.expandvars(env_dir))) / "projects"
    return Path.home() / ".claude" / "projects"
