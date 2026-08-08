"""Single source of truth for the Better Agent state directory.

All persistence modules (`session_store`, `worker_store`,
`pending_approvals`, `project_store`, `provider_claude`,
`trace_collector`, `orchestrator`'s internal
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

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

try:  # POSIX only; absent on Windows.
    import pwd as _pwd
except ImportError:  # pragma: no cover - non-POSIX
    _pwd = None


_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_UMASK = 0o077
_SECURED_ROOTS: set[str] = set()
_WINDOWS_CURRENT_USER_SID: str | None = None
_WINDOWS_SECURITY: _WindowsSecurity | None = None
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


def expand_user_path(raw: str) -> Path:
    expanded = os.path.expandvars(raw)
    if expanded == "~":
        return user_home()
    if expanded.startswith("~/"):
        return user_home() / expanded[2:]
    return Path(expanded)


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


class _WindowsSecurity:  # pragma: no cover - Win32 ctypes ACL; WinDLL absent on POSIX test platform, faking it gives no real security confidence
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SECURITY_DESCRIPTOR_REVISION = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _ERROR_INSUFFICIENT_BUFFER = 122
    _ACCESS_ALLOWED_ACE_TYPE = 0x00
    _OTHER_ALLOW_ACE_TYPES = frozenset({0x04, 0x05, 0x09, 0x0B})

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("Sid", ctypes.c_void_p),
                ("Attributes", wintypes.DWORD),
            ]

        class TokenUser(ctypes.Structure):
            _fields_ = [("User", SidAndAttributes)]

        class Acl(ctypes.Structure):
            _fields_ = [
                ("AclRevision", ctypes.c_ubyte),
                ("Sbz1", ctypes.c_ubyte),
                ("AclSize", wintypes.WORD),
                ("AceCount", wintypes.WORD),
                ("Sbz2", wintypes.WORD),
            ]

        self.TokenUser = TokenUser
        self.Acl = Acl
        self._configure_functions()

    def _configure_functions(self) -> None:
        ctypes = self.ctypes
        wintypes = self.wintypes
        advapi32 = self.advapi32
        kernel32 = self.kernel32

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSidToSidW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetAce.restype = wintypes.BOOL
        advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi32.EqualSid.restype = wintypes.BOOL

    def _raise_last_error(self, operation: str) -> None:
        error = self.ctypes.get_last_error()
        raise OSError(error, operation)

    def _local_free(self, pointer: object) -> None:
        if pointer:
            self.kernel32.LocalFree(
                self.ctypes.cast(pointer, self.ctypes.c_void_p),
            )

    def current_user_sid(self) -> str:
        ctypes = self.ctypes
        wintypes = self.wintypes
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            self._TOKEN_QUERY,
            ctypes.byref(token),
        ):
            self._raise_last_error("OpenProcessToken failed")
        try:
            size = wintypes.DWORD()
            ctypes.set_last_error(0)
            sized = self.advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                None,
                0,
                ctypes.byref(size),
            )
            if (
                sized
                or size.value == 0
                or ctypes.get_last_error()
                != self._ERROR_INSUFFICIENT_BUFFER
            ):
                self._raise_last_error("GetTokenInformation size failed")
            buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                size,
                ctypes.byref(size),
            ):
                self._raise_last_error("GetTokenInformation failed")
            user = ctypes.cast(
                buffer,
                ctypes.POINTER(self.TokenUser),
            ).contents
            string_sid = ctypes.c_wchar_p()
            if not self.advapi32.ConvertSidToStringSidW(
                user.User.Sid,
                ctypes.byref(string_sid),
            ):
                self._raise_last_error("ConvertSidToStringSidW failed")
            try:
                value = string_sid.value
                if not value:
                    raise OSError("current Windows user SID is empty")
                return value
            finally:
                self._local_free(string_sid)
        finally:
            self.kernel32.CloseHandle(token)

    def _sid_pointer(self, value: str) -> object:
        pointer = self.ctypes.c_void_p()
        if not self.advapi32.ConvertStringSidToSidW(
            value,
            self.ctypes.byref(pointer),
        ):
            self._raise_last_error("ConvertStringSidToSidW failed")
        return pointer

    def apply_private_acl(
        self,
        path: Path,
        *,
        user_sid: str,
        directory: bool,
    ) -> None:
        ctypes = self.ctypes
        wintypes = self.wintypes
        inheritance = "OICI" if directory else ""
        descriptor_text = (
            "D:P"
            f"(A;{inheritance};FA;;;{user_sid})"
            f"(A;{inheritance};FA;;;SY)"
            f"(A;{inheritance};FA;;;BA)"
        )
        descriptor = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor_text,
            self._SECURITY_DESCRIPTOR_REVISION,
            ctypes.byref(descriptor),
            None,
        ):
            self._raise_last_error(
                "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
            )
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not self.advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ) or not present.value or not dacl.value:
                raise OSError("private Windows DACL is unavailable")
            error = self.advapi32.SetNamedSecurityInfoW(
                str(path),
                self._SE_FILE_OBJECT,
                (
                    self._DACL_SECURITY_INFORMATION
                    | self._PROTECTED_DACL_SECURITY_INFORMATION
                ),
                None,
                None,
                dacl,
                None,
            )
            if error:
                raise OSError(error, "SetNamedSecurityInfoW failed")
        finally:
            self._local_free(descriptor)

    def has_private_acl(self, path: Path, *, user_sid: str) -> bool:
        ctypes = self.ctypes
        wintypes = self.wintypes
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        error = self.advapi32.GetNamedSecurityInfoW(
            str(path),
            self._SE_FILE_OBJECT,
            (
                self._OWNER_SECURITY_INFORMATION
                | self._DACL_SECURITY_INFORMATION
            ),
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if error:
            raise OSError(error, "GetNamedSecurityInfoW failed")
        sid_pointers: list[object] = []
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if (
                not owner.value
                or not dacl.value
                or not self.advapi32.GetSecurityDescriptorControl(
                    descriptor,
                    ctypes.byref(control),
                    ctypes.byref(revision),
                )
                or not control.value & self._SE_DACL_PROTECTED
            ):
                return False
            sid_pointers = [
                self._sid_pointer(value)
                for value in (
                    user_sid,
                    "S-1-5-18",
                    "S-1-5-32-544",
                )
            ]
            if not any(
                self.advapi32.EqualSid(owner, allowed)
                for allowed in sid_pointers
            ):
                return False
            acl = ctypes.cast(
                dacl,
                ctypes.POINTER(self.Acl),
            ).contents
            current_user_allowed = False
            for index in range(acl.AceCount):
                ace = ctypes.c_void_p()
                if not self.advapi32.GetAce(
                    dacl,
                    index,
                    ctypes.byref(ace),
                ):
                    return False
                ace_type = ctypes.cast(
                    ace,
                    ctypes.POINTER(ctypes.c_ubyte),
                ).contents.value
                if ace_type in self._OTHER_ALLOW_ACE_TYPES:
                    return False
                if ace_type != self._ACCESS_ALLOWED_ACE_TYPE:
                    continue
                ace_sid = ctypes.c_void_p(ace.value + 8)
                if not any(
                    self.advapi32.EqualSid(ace_sid, allowed)
                    for allowed in sid_pointers
                ):
                    return False
                if self.advapi32.EqualSid(ace_sid, sid_pointers[0]):
                    current_user_allowed = True
            return current_user_allowed
        finally:
            for sid in sid_pointers:
                self._local_free(sid)
            self._local_free(descriptor)


def _windows_security() -> _WindowsSecurity:  # pragma: no cover - Windows-only (see _WindowsSecurity)
    global _WINDOWS_SECURITY
    if _WINDOWS_SECURITY is None:
        _WINDOWS_SECURITY = _WindowsSecurity()
    return _WINDOWS_SECURITY


def _windows_current_user_sid() -> str:  # pragma: no cover - Windows-only (see _WindowsSecurity)
    global _WINDOWS_CURRENT_USER_SID
    if _WINDOWS_CURRENT_USER_SID is None:
        _WINDOWS_CURRENT_USER_SID = _windows_security().current_user_sid()
    return _WINDOWS_CURRENT_USER_SID


def _set_windows_private_acl(path: Path, *, directory: bool) -> None:  # pragma: no cover - Windows-only (see _WindowsSecurity)
    _windows_security().apply_private_acl(
        path,
        user_sid=_windows_current_user_sid(),
        directory=directory,
    )


def _require_non_redirecting_path(path: Path, *, directory: bool) -> None:
    try:
        observed = path.lstat()
        junction = bool(
            getattr(path, "is_junction", lambda: False)(),
        )
    except OSError as exc:
        raise PermissionError("private path is unavailable") from exc
    expected_type = (
        stat.S_ISDIR(observed.st_mode)
        if directory
        else stat.S_ISREG(observed.st_mode)
    )
    if not expected_type or stat.S_ISLNK(observed.st_mode) or junction:
        raise PermissionError("private path must not redirect")


def make_private_file(path: Path) -> None:
    _require_non_redirecting_path(path, directory=False)
    if os.name == "nt":
        if windows_path_has_private_acl(path):
            return
        _set_windows_private_acl(path, directory=False)
        if not windows_path_has_private_acl(path):
            raise PermissionError("private file ACL verification failed")
        return
    path.chmod(0o600)


def require_private_file(path: Path) -> None:
    _require_non_redirecting_path(path, directory=False)
    if os.name == "nt":
        if not windows_path_has_private_acl(path):
            raise PermissionError("private file ACL verification failed")
        return
    observed = path.lstat()
    if (
        stat.S_IMODE(observed.st_mode) & 0o077
        or (_pwd is not None and observed.st_uid != os.getuid())
    ):
        raise PermissionError("private file permissions are unsafe")


def windows_path_has_private_acl(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        return _windows_security().has_private_acl(
            path,
            user_sid=_windows_current_user_sid(),
        )
    except (
        AttributeError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return False


def make_private_directory(path: Path) -> None:
    _require_non_redirecting_path(path, directory=True)
    if os.name == "nt":
        if windows_path_has_private_acl(path):
            return
        try:
            _set_windows_private_acl(path, directory=True)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            raise PermissionError(
                "private directory ACL update failed",
            ) from exc
        if not windows_path_has_private_acl(path):
            raise PermissionError("private directory ACL verification failed")
        return
    observed = path.lstat()
    if _pwd is not None and observed.st_uid != os.getuid():
        raise PermissionError("private directory owner is unsafe")
    if stat.S_IMODE(observed.st_mode) == _PRIVATE_DIR_MODE:
        return
    path.chmod(_PRIVATE_DIR_MODE)


def require_private_directory(path: Path) -> None:
    _require_non_redirecting_path(path, directory=True)
    if os.name == "nt":
        if not windows_path_has_private_acl(path):
            raise PermissionError("private directory ACL verification failed")
        return
    observed = path.lstat()
    if (
        stat.S_IMODE(observed.st_mode) & 0o077
        or (_pwd is not None and observed.st_uid != os.getuid())
    ):
        raise PermissionError("private directory permissions are unsafe")


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


_HOME_CACHE: dict[tuple[str, str], Path] = {}
_HOME_CACHE_LOCK = threading.Lock()


def reset_home_cache() -> None:
    """Drop the memoized ``ba_home()`` result and the secured-roots set.

    The env-keyed cache below already re-resolves automatically whenever the
    home env vars change (the common test case), so call this only when a test
    mutates on-disk state under an *unchanged* env value and wants the next
    ``ba_home()`` to redo the mkdir / chmod / alias work from scratch.
    """
    with _HOME_CACHE_LOCK:
        _HOME_CACHE.clear()
        _SECURED_ROOTS.clear()


def _record_resolve_ms(ms: float) -> None:
    """Surface cold-path resolves in the standard PERF rollup so we can verify
    in production that the cache is doing its job: after startup this counter
    should stay near-flat (resolves only on first-touch per env). Lazy import +
    swallow everything — `paths` is imported far too early and broadly to take
    a hard dependency on `perf`, and instrumentation must never break path
    resolution."""
    try:
        import perf
        perf.record("paths.ba_home.resolve", ms)
    except Exception:
        pass


def _resolve_home_uncached() -> Path:
    import time as _time
    t0 = _time.perf_counter()
    configured = _env_home(_PRIMARY_HOME_ENV) or _env_home(_LEGACY_HOME_ENV)
    root = configured or _default_home()
    assert_state_root_safe(root)
    root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if configured is None and root.name == _DEFAULT_STATE_DIR:
        _ensure_default_alias(root)
    secured_key = str(root.resolve())
    if secured_key not in _SECURED_ROOTS:
        make_private_directory(root)
        _SECURED_ROOTS.add(secured_key)
    _record_resolve_ms((_time.perf_counter() - t0) * 1000.0)
    return root


def ba_home() -> Path:
    """Resolve the Better Agent state root.

    Honors `BETTER_AGENT_HOME` if set, then `BETTER_CLAUDE_HOME` as
    legacy fallback. Env values must be absolute. Without env config,
    defaults to `~/.better-claude`; when that path is used, creates
    `~/.better-agent` as a local alias if possible.

    HOT PATH: this is called hundreds of times across the backend —
    including inside per-request auth resolution
    (`orchestrator.resolve_principal`) — and the full resolve issues
    `mkdir` + `realpath` + `chmod` syscalls every time even though the
    answer is process-stable. Run on the asyncio loop that made it a
    top event-loop blocker (faulthandler dumps showed `ba_home` as the
    single most frequent blocking frame). So memoize the result keyed by
    the env inputs that are the only thing that can change it. The key is
    read fresh from the environment on every call (NOT cached at import),
    so a test that swaps `BETTER_AGENT_HOME`/`BETTER_CLAUDE_HOME`
    mid-process still re-resolves — preserving the contract in this
    module's docstring. Steady-state calls are now syscall-free.
    """
    env_primary = os.environ.get(_PRIMARY_HOME_ENV, "").strip()
    env_legacy = os.environ.get(_LEGACY_HOME_ENV, "").strip()
    cache_key = (env_primary, env_legacy)
    cached = _HOME_CACHE.get(cache_key)
    if cached is not None:
        return cached
    # Cold path: serialize first-resolve per env so concurrent callers
    # don't all run the mkdir/chmod/alias setup. Double-check inside the
    # lock. The body never re-enters ba_home(), so no deadlock.
    with _HOME_CACHE_LOCK:
        cached = _HOME_CACHE.get(cache_key)
        if cached is not None:
            return cached
        root = _resolve_home_uncached()
        _HOME_CACHE[cache_key] = root
        return root


# Alias to satisfy requirements extension and global rules.
bc_home = ba_home


def engage_test_home(home: str) -> str:
    """Single source of truth for the test-home engagement contract.

    Sets PRIMARY (`BETTER_AGENT_HOME`) and LEGACY (`BETTER_CLAUDE_HOME`) to the
    same tempdir, plus `BETTER_AGENT_TEST_MODE`. PRIMARY must be set because
    `_resolve_home_uncached` checks it FIRST — a dev shell that permanently
    exports a real `BETTER_AGENT_HOME` otherwise shadows the legacy var and
    routes test writes into production state. TEST_MODE arms
    `assert_state_root_safe` so any later resolve that still lands on a
    production home fails closed instead of silently clobbering it.

    `_test_home.engage` and the requirements extension tests both route through
    here so the env shape cannot drift between callers. Must be called BEFORE
    importing any backend module that may resolve `ba_home()` at import time.
    Importing `paths` itself is safe — it does not resolve home at module load.
    """
    resolved = str(Path(home).expanduser().resolve())
    os.environ[_PRIMARY_HOME_ENV] = resolved
    os.environ[_LEGACY_HOME_ENV] = resolved
    os.environ[_TEST_MODE_ENV] = "1"
    return resolved



_ENCODE_CWD_MAX_LEN = 200  # claude CLI's own project-dir-name cap (`RA()`)

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _js_utf16_code_units(s: str) -> list[int]:
    """`s`'s UTF-16 code units, matching JS string indexing/`charCodeAt`
    (astral characters split into surrogate pairs) so `encode_cwd` replaces
    and hashes byte-for-byte like the CLI's own JS implementation does."""
    encoded = s.encode("utf-16-le")
    return [encoded[i] | (encoded[i + 1] << 8) for i in range(0, len(encoded), 2)]


def _js_ascii_alnum_dash(s: str) -> str:
    """Port of claude CLI's `e.replace(/[^a-zA-Z0-9]/g,"-")`: every UTF-16
    code unit outside ASCII `[a-zA-Z0-9]` becomes `-` (Python's `str.isalnum`
    is unicode-aware and would keep characters the CLI's regex would not)."""
    out = []
    for code in _js_utf16_code_units(s):
        if (0x30 <= code <= 0x39) or (0x41 <= code <= 0x5A) or (0x61 <= code <= 0x7A):
            out.append(chr(code))
        else:
            out.append("-")
    return "".join(out)


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = []
    while n:
        n, rem = divmod(n, 36)
        digits.append(_BASE36_DIGITS[rem])
    return "".join(reversed(digits))


def _js_string_hashcode32(s: str) -> int:
    """Port of claude CLI's `art()` string hashcode (`t = (t<<5)-t+code|0`,
    i.e. `t = t*31 + code` wrapped to a signed 32-bit int every step —
    JS `|0` semantics) over `s`'s UTF-16 code units. Feeds the `-<hash>`
    suffix `encode_cwd` appends past `_ENCODE_CWD_MAX_LEN`."""
    h = 0
    for code in _js_utf16_code_units(s):
        h = h * 31 + code
        h &= 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return h


def encode_cwd(cwd: str) -> str:
    """Normalize a cwd into a filesystem-safe token.

    Byte-for-byte port of claude CLI's own `~/.claude/projects/<token>/`
    directory-name encoder (`RA()` in the installed CLI bundle, verified
    by extracting it from the shipped binary): replace every character
    that is not ASCII `[a-zA-Z0-9]` with `-` — NOT just `/`, `\\`, `:`,
    `_` as an earlier version of this function assumed. That included
    `.`, spaces, `~`, and everything else non-alphanumeric: a cwd like
    `/foo/.claude/bar` encodes to `-foo--claude-bar` (double dash — one
    `-` for the path separator, one for the literal `.`), not
    `-foo-.claude-bar`. Getting this wrong means every claude-CLI-jsonl
    consumer (the live tailer, resume-path lookup, memory store, native
    session search/miner, extension project ids, …) watches or looks up
    a directory claude CLI never writes to, and silently sees nothing.

    Also replicates the CLI's length cap: past `_ENCODE_CWD_MAX_LEN`
    characters the token is truncated and suffixed with `-<hash>`, where
    `<hash>` is `abs(art(cwd)).toString(36)` (`art` = JS's classic
    `h*31 + charCode` string hashcode, 32-bit wrapped) — without this a
    long cwd's encoded token diverges from the CLI's past the cap.
    """
    resolved = Path(cwd).expanduser().resolve().as_posix()
    token = _js_ascii_alnum_dash(resolved)
    if not token:
        return "root"
    if len(token) <= _ENCODE_CWD_MAX_LEN:
        return token
    digest = _to_base36(abs(_js_string_hashcode32(resolved)))
    return f"{token[:_ENCODE_CWD_MAX_LEN]}-{digest}"


def resolve_provider_config_dir(cfg_dir: str) -> Path:
    """Expand a provider `config_dir` to an absolute path.

    Relative paths anchor to the pwd-derived user home (never the
    spoofable `$HOME` env var). A leading `$HOME/` / `${HOME}/` is
    normalized to `~/` so it too anchors to the real home. Shared by
    every provider that isolates a per-account credential directory via
    an env var (claude `CLAUDE_CONFIG_DIR`, codex `CODEX_HOME`): without
    this, a value like `.codex-work` resolves against whatever cwd each
    consumer runs with, scattering a per-project store the backend never
    finds.
    """
    normalized = str(cfg_dir)
    if normalized.startswith("$HOME/"):
        normalized = "~/" + normalized[len("$HOME/"):]
    elif normalized.startswith("${HOME}/"):
        normalized = "~/" + normalized[len("${HOME}/"):]
    path = expand_user_path(normalized)
    if not path.is_absolute():
        path = user_home() / path
    return path


def resolve_claude_config_dir(cfg_dir: str) -> Path:
    """Claude-facing alias of `resolve_provider_config_dir`; kept for the
    `CLAUDE_CONFIG_DIR` call sites (projects-root resolution, ingestion)."""
    return resolve_provider_config_dir(cfg_dir)


def scheme_home(component: str, version: int) -> Path:
    """Schema-versioned state directory for `component` at `version`.

    Returns `<ba_home()>/scheme/<component>/v<version>/`, creating every
    path segment under `ba_home()` as a private (owner-only) directory —
    `ba_home()` only secures its own root, not descendants. Idempotent:
    calling again for an already-created dir just re-verifies privacy.

    `component` must be a bare token (no path separators, no `.`/`..`) so
    a caller can never escape the `scheme/` root via traversal.

    This is the on-disk root for `scheme_migrations.ensure()` — see
    `backend/scheme_migrations.py` for the versioning/migration contract
    built on top of this path.
    """
    if (
        not component
        or component in (".", "..")
        or "/" in component
        or "\\" in component
    ):
        raise ValueError(f"scheme_home: invalid component {component!r}")
    if version < 1:
        raise ValueError(f"scheme_home: version must be >= 1, got {version}")
    root = ba_home()
    target = root / "scheme" / component / f"v{version}"
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        try:
            current.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=False)
        except FileExistsError:
            require_private_directory(current)
            continue
        make_private_directory(current)
        require_private_directory(current)
    return target


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
                return resolve_claude_config_dir(cfg_dir) / "projects"
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if env_dir:
        return resolve_claude_config_dir(env_dir) / "projects"
    return Path.home() / ".claude" / "projects"
