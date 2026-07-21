from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from json_store import read_json, write_json
from paths import ba_home, prod_state_roots, user_home


_SCHEMA_VERSION = 1
_DEFAULT_USER = "betteragent"
_DEFAULT_GROUP = "betteragent"
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_RUNNER_LEGACY_ENV_KEYS = {
    "BETTER_CLAUDE_BACKEND_URL",
    "BETTER_CLAUDE_INTERNAL_TOKEN",
    "BETTER_CLAUDE_APP_SESSION_ID",
    "BETTER_CLAUDE_CWD",
    "BETTER_CLAUDE_MODEL",
    "BETTER_CLAUDE_PROVIDER_ID",
    "BETTER_CLAUDE_BARE_CONFIG",
    "BETTER_CLAUDE_USER_FACING",
    "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS",
}
_PRESERVED_ENV_KEYS = {
    *(_RUNNER_LEGACY_ENV_KEYS),
    *(key.replace("BETTER_CLAUDE_", "BETTER_AGENT_", 1) for key in _RUNNER_LEGACY_ENV_KEYS),
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "SAKANA_API_KEY",
    "GEMINI_API_KEY",
    "GEMINI_CLI_HOME",
    "GOOGLE_API_KEY",
    "PATH",
    "TMPDIR",
}


@dataclass(frozen=True)
class RuntimeConfig:
    isolated_user_enabled: bool
    username: str
    group: str


@dataclass(frozen=True)
class RuntimeCommand:
    argv: tuple[str, ...]
    requires_privilege: bool = True


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _config_path() -> Path:
    return ba_home() / "provider_runtime.json"


def _validate_account_name(value: str, field: str) -> str:
    candidate = str(value or "").strip()
    if not _NAME_RE.match(candidate):
        raise ValueError(f"invalid {field}")
    return candidate


def load_config() -> RuntimeConfig:
    raw = read_json(_config_path(), {})
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA_VERSION:
        return RuntimeConfig(False, _DEFAULT_USER, _DEFAULT_GROUP)
    enabled = raw.get("isolated_user_enabled") is True
    username = raw.get("username") if isinstance(raw.get("username"), str) else _DEFAULT_USER
    group = raw.get("group") if isinstance(raw.get("group"), str) else _DEFAULT_GROUP
    try:
        return RuntimeConfig(
            enabled,
            _validate_account_name(username, "username"),
            _validate_account_name(group, "group"),
        )
    except ValueError:
        return RuntimeConfig(False, _DEFAULT_USER, _DEFAULT_GROUP)


def save_config(config: RuntimeConfig) -> RuntimeConfig:
    username = _validate_account_name(config.username, "username")
    group = _validate_account_name(config.group, "group")
    normalized = RuntimeConfig(bool(config.isolated_user_enabled), username, group)
    write_json(
        _config_path(),
        {
            "schema": _SCHEMA_VERSION,
            "isolated_user_enabled": normalized.isolated_user_enabled,
            "username": normalized.username,
            "group": normalized.group,
        },
    )
    return normalized


def enable_isolated_user(username: str = _DEFAULT_USER, group: str = _DEFAULT_GROUP) -> RuntimeConfig:
    return save_config(RuntimeConfig(True, username, group))


def disable_isolated_user() -> RuntimeConfig:
    current = load_config()
    return save_config(RuntimeConfig(False, current.username, current.group))


def isolated_user_enabled() -> bool:
    return load_config().isolated_user_enabled


def _preserved_env_keys(env: dict[str, str] | None) -> tuple[str, ...]:
    if not env:
        return ()
    keys = []
    for key in env:
        if key in _PRESERVED_ENV_KEYS:
            keys.append(key)
    return tuple(sorted(keys))


def runner_spawn_argv(argv: Sequence[str], *, env: dict[str, str] | None = None) -> tuple[str, ...]:
    config = load_config()
    base = tuple(str(part) for part in argv)
    if not config.isolated_user_enabled:
        return base
    system = platform.system().lower()
    if system not in ("darwin", "linux"):
        raise RuntimeError(f"isolated provider runs are unsupported on {platform.system()}")
    preserved = _preserved_env_keys(env)
    preserve_arg = (f"--preserve-env={','.join(preserved)}",) if preserved else ()
    return ("sudo", "-n", "-H", *preserve_arg, "-u", config.username, "--", *base)


def popen_runner(
    argv: Sequence[str],
    *,
    run_dir: Path,
    project_cwd: str,
    **kwargs,
) -> subprocess.Popen:
    config = load_config()
    if config.isolated_user_enabled:
        grant_runtime_path_access(run_dir, group=config.group)
        if project_access_allowed(project_cwd):
            grant_project_access(project_cwd, group=config.group)
    env = kwargs.get("env")
    return subprocess.Popen(
        runner_spawn_argv(argv, env=env if isinstance(env, dict) else None),
        **kwargs,
    )


def _current_username() -> str:
    import getpass

    return getpass.getuser()


def _macos_create_user_commands(username: str, group: str, current_user: str) -> list[RuntimeCommand]:
    home = f"/Users/{username}"
    return [
        RuntimeCommand(("dscl", ".", "-read", f"/Groups/{group}"), False),
        RuntimeCommand(("dseditgroup", "-o", "create", group)),
        RuntimeCommand(("dseditgroup", "-o", "edit", "-a", current_user, "-t", "user", group)),
        RuntimeCommand(("dscl", ".", "-read", f"/Users/{username}"), False),
        RuntimeCommand(("sysadminctl", "-addUser", username, "-home", home, "-shell", "/bin/zsh")),
        RuntimeCommand(("dseditgroup", "-o", "edit", "-a", username, "-t", "user", group)),
        RuntimeCommand(("createhomedir", "-c", "-u", username)),
    ]


def _linux_create_user_commands(username: str, group: str, current_user: str) -> list[RuntimeCommand]:
    return [
        RuntimeCommand(("getent", "group", group), False),
        RuntimeCommand(("groupadd", group)),
        RuntimeCommand(("id", "-u", username), False),
        RuntimeCommand(("useradd", "--create-home", "--shell", "/bin/bash", "--gid", group, username)),
        RuntimeCommand(("usermod", "-aG", group, current_user)),
        RuntimeCommand(("usermod", "-aG", group, username)),
    ]


def plan_isolated_user_commands(
    username: str = _DEFAULT_USER,
    group: str = _DEFAULT_GROUP,
    *,
    current_user: str | None = None,
) -> list[RuntimeCommand]:
    username = _validate_account_name(username, "username")
    group = _validate_account_name(group, "group")
    actor = current_user or _current_username()
    system = platform.system().lower()
    if system == "darwin":
        return _macos_create_user_commands(username, group, actor)
    if system == "linux":
        return _linux_create_user_commands(username, group, actor)
    raise RuntimeError(f"isolated users are unsupported on {platform.system()}")


def _run_command(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def apply_isolated_user(
    username: str = _DEFAULT_USER,
    group: str = _DEFAULT_GROUP,
    *,
    current_user: str | None = None,
    verify_run_as: bool = True,
    runner: CommandRunner = _run_command,
) -> RuntimeConfig:
    username = _validate_account_name(username, "username")
    group = _validate_account_name(group, "group")
    actor = current_user or _current_username()
    system = platform.system().lower()
    if system == "darwin":
        _apply_macos_isolated_user(username, group, actor, runner)
    elif system == "linux":
        _apply_linux_isolated_user(username, group, actor, runner)
    else:
        raise RuntimeError(f"isolated users are unsupported on {platform.system()}")
    if verify_run_as:
        _ensure_run_as_available(username, runner)
    return enable_isolated_user(username, group)


def _command_error(argv: Sequence[str], result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return f"failed to run {argv[0]}: {text or result.returncode}"


def _must_run(argv: Sequence[str], runner: CommandRunner) -> None:
    result = runner(argv)
    if result.returncode != 0:
        raise RuntimeError(_command_error(argv, result))


def _ensure_run_as_available(username: str, runner: CommandRunner) -> None:
    result = runner(("sudo", "-n", "-H", "-u", username, "--", "true"))
    if result.returncode != 0:
        raise RuntimeError(
            "noninteractive run-as is not available for isolated provider runs"
        )


def _apply_macos_isolated_user(
    username: str,
    group: str,
    current_user: str,
    runner: CommandRunner,
) -> None:
    if runner(("dscl", ".", "-read", f"/Groups/{group}")).returncode != 0:
        _must_run(("dseditgroup", "-o", "create", group), runner)
    _must_run(("dseditgroup", "-o", "edit", "-a", current_user, "-t", "user", group), runner)
    if runner(("dscl", ".", "-read", f"/Users/{username}")).returncode != 0:
        _must_run(("sysadminctl", "-addUser", username, "-home", f"/Users/{username}", "-shell", "/bin/zsh"), runner)
        _must_run(("createhomedir", "-c", "-u", username), runner)
    _must_run(("dseditgroup", "-o", "edit", "-a", username, "-t", "user", group), runner)


def _apply_linux_isolated_user(
    username: str,
    group: str,
    current_user: str,
    runner: CommandRunner,
) -> None:
    if runner(("getent", "group", group)).returncode != 0:
        _must_run(("groupadd", group), runner)
    if runner(("id", "-u", username)).returncode != 0:
        _must_run(("useradd", "--create-home", "--shell", "/bin/bash", "--gid", group, username), runner)
    _must_run(("usermod", "-aG", group, current_user), runner)
    _must_run(("usermod", "-aG", group, username), runner)


def _project_path(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError("project path must be an existing directory")
    if not project_access_allowed(resolved):
        raise ValueError(f"refusing broad or sensitive project path: {resolved}")
    return resolved


def project_access_allowed(path: str | Path) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if not resolved.is_dir():
        return False
    if resolved == resolved.anchor:
        return False
    if resolved.parent == Path(resolved.anchor):
        return False
    denied_roots = [
        Path("/tmp"),
        Path("/private"),
        Path("/private/tmp"),
        Path("/var/tmp"),
        Path("/Applications"),
        Path("/Library"),
        Path("/System"),
    ]
    if any(resolved == root.resolve() for root in denied_roots if root.exists()):
        return False
    home = user_home().resolve()
    if resolved == home:
        return False
    for state_root in prod_state_roots():
        try:
            state = state_root.resolve()
        except OSError:
            state = state_root
        if resolved == state or state in resolved.parents:
            return False
    return True


def _runtime_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    runs_root = (ba_home() / "runs").resolve()
    if not resolved.is_dir():
        raise ValueError("runtime path must be an existing directory")
    if resolved != runs_root and runs_root not in resolved.parents:
        raise ValueError(f"refusing runtime ACL outside runs root: {resolved}")
    return resolved


def _macos_project_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    acl = (
        f"group:{group} allow list,search,readattr,readextattr,readsecurity,"
        "file_inherit,directory_inherit,read,write,append,delete,"
        "add_file,add_subdirectory,delete_child,writeattr,writeextattr"
    )
    return [RuntimeCommand(("chmod", "-R", "+a", acl, str(path)))]


def _macos_search_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    acl = f"group:{group} allow search,readattr,readextattr,readsecurity"
    return [RuntimeCommand(("chmod", "+a", acl, str(path)))]


def _linux_project_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    if not shutil.which("setfacl"):
        raise RuntimeError("setfacl is required for isolated-user project access on Linux")
    return [
        RuntimeCommand(("setfacl", "-R", "-m", f"g:{group}:rwX", str(path))),
        RuntimeCommand(("setfacl", "-R", "-d", "-m", f"g:{group}:rwX", str(path))),
    ]


def _linux_search_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    if not shutil.which("setfacl"):
        raise RuntimeError("setfacl is required for isolated-user runtime access on Linux")
    return [RuntimeCommand(("setfacl", "-m", f"g:{group}:--x", str(path)))]


def plan_project_access_commands(path: str, group: str = _DEFAULT_GROUP) -> list[RuntimeCommand]:
    group = _validate_account_name(group, "group")
    project = _project_path(path)
    system = platform.system().lower()
    if system == "darwin":
        return _macos_project_access_commands(project, group)
    if system == "linux":
        return _linux_project_access_commands(project, group)
    raise RuntimeError(f"project access sync is unsupported on {platform.system()}")


def grant_project_access(
    path: str,
    *,
    group: str = _DEFAULT_GROUP,
    runner: CommandRunner = _run_command,
) -> None:
    for command in plan_project_access_commands(path, group):
        result = runner(command.argv)
        if result.returncode != 0:
            raise RuntimeError(_command_error(command.argv, result))


def grant_runtime_path_access(
    path: str | Path,
    *,
    group: str = _DEFAULT_GROUP,
    runner: CommandRunner = _run_command,
) -> None:
    runtime_path = _runtime_path(path)
    group = _validate_account_name(group, "group")
    ancestors = [ba_home().resolve(), (ba_home() / "runs").resolve()]
    commands: list[RuntimeCommand] = []
    for ancestor in ancestors:
        commands.extend(_search_access_commands(ancestor, group))
    commands.extend(_project_access_commands(runtime_path, group))
    for command in commands:
        result = runner(command.argv)
        if result.returncode != 0:
            raise RuntimeError(_command_error(command.argv, result))


def grant_project_access_if_enabled(project: dict) -> bool:
    config = load_config()
    if not config.isolated_user_enabled:
        return False
    if (project.get("node_id") or "primary") != "primary":
        return False
    path = project.get("path")
    if not isinstance(path, str) or not path:
        return False
    if not project_access_allowed(path):
        return False
    grant_project_access(path, group=config.group)
    return True


def sync_loaded_project_access(projects: Iterable[dict]) -> int:
    changed = 0
    for project in projects:
        if grant_project_access_if_enabled(project):
            changed += 1
    return changed


def _project_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    system = platform.system().lower()
    if system == "darwin":
        return _macos_project_access_commands(path, group)
    if system == "linux":
        return _linux_project_access_commands(path, group)
    raise RuntimeError(f"project access sync is unsupported on {platform.system()}")


def _search_access_commands(path: Path, group: str) -> list[RuntimeCommand]:
    system = platform.system().lower()
    if system == "darwin":
        return _macos_search_access_commands(path, group)
    if system == "linux":
        return _linux_search_access_commands(path, group)
    raise RuntimeError(f"runtime access sync is unsupported on {platform.system()}")
