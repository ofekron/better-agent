import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlsplit

from better_agent_sdk import BetterAgentError, Client
from better_agent_sdk.surfaces import OperationSpec, build_cli_app, build_mcp_server


SUPPORTED_CLI_VERSION = "2.0.15"
_MAX_OUTPUT = 100_000
_MAX_CONTENT = 20_000
_MAX_JSON = 100_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_MEDIA_SUFFIXES = {".gif", ".jpeg", ".jpg", ".mp4", ".png"}
_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


class PostizError(ValueError):
    def __init__(self, message: str, *, kind: str = "invalid_input") -> None:
        super().__init__(message)
        self.kind = kind


class PostBlock(TypedDict):
    content: str
    media: NotRequired[list[str]]


def _settings() -> dict[str, Any]:
    try:
        response = Client().get_settings()
    except BetterAgentError:
        raise PostizError(
            "Postiz extension settings are unavailable",
            kind="settings_unavailable",
        ) from None
    values = response.get("settings")
    if response.get("success") is not True or not isinstance(values, dict):
        raise PostizError(
            "Postiz extension settings are unavailable",
            kind="settings_unavailable",
        )
    return values


def _validate_api_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PostizError("api_url must not contain credentials, query parameters, or fragments")
    if not parsed.hostname or parsed.path not in {"", "/"}:
        raise PostizError("api_url must contain only a scheme and host")
    loopback = parsed.hostname in {"127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise PostizError("api_url must use HTTPS, except for literal loopback hosts")
    return url


def _base_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWLIST
    }


def _minimal_env(settings: dict[str, Any], isolated_home: str | None = None) -> dict[str, str]:
    env = _base_env()
    mode = str(settings.get("auth_mode") or "oauth")
    if mode == "oauth":
        for key in ("HOME", "USERPROFILE"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        return env
    if mode != "api_key":
        raise PostizError("auth_mode must be oauth or api_key")
    api_key = str(settings.get("api_key") or "")
    if not api_key:
        raise PostizError("api_key is required when auth_mode is api_key")
    if isolated_home is None:
        raise PostizError("isolated API-key home is required")
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    env["POSTIZ_API_KEY"] = api_key
    env["POSTIZ_API_URL"] = _validate_api_url(
        str(settings.get("api_url") or "https://api.postiz.com")
    )
    return env


def _redact(value: str, secrets: list[str]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result[:_MAX_OUTPUT]


def _extract_json(stdout: str) -> Any | None:
    decoder = json.JSONDecoder()
    for index in range(len(stdout) - 1, -1, -1):
        if stdout[index] not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not stdout[index + end :].strip():
            return value
    return None


def _find_cli() -> list[str]:
    executable = shutil.which("postiz")
    if not executable:
        raise PostizError(
            f"Postiz CLI {SUPPORTED_CLI_VERSION} is required on PATH",
            kind="dependency_missing",
        )
    path = Path(executable)
    if path.suffix.lower() not in {".bat", ".cmd"}:
        return [executable]
    node = shutil.which("node")
    entrypoint = path.parent / "node_modules" / "postiz" / "dist" / "index.js"
    if not node or not entrypoint.is_file():
        raise PostizError(
            "Postiz CLI Windows installation is incomplete",
            kind="dependency_incomplete",
        )
    return [node, str(entrypoint)]


def _execute(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    secrets: list[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        detail = _redact(output, secrets).strip()
        suffix = f": {detail}" if detail else ""
        raise PostizError(
            f"Postiz CLI timed out{suffix}",
            kind="timeout",
        ) from None
    except OSError:
        raise PostizError(
            "Postiz CLI could not be started",
            kind="spawn_failed",
        ) from None


def _run(argv: list[str]) -> dict[str, Any]:
    try:
        return _run_checked(argv)
    except PostizError as exc:
        return {
            "success": False,
            "error_kind": exc.kind,
            "error": str(exc),
        }


def _run_checked(argv: list[str]) -> dict[str, Any]:
    settings = _settings()
    secret = str(settings.get("api_key") or "")
    command = _find_cli()
    secrets = [secret]
    with tempfile.TemporaryDirectory(prefix="better-agent-postiz-") as isolated_home:
        version = _execute(
            [*command, "--version"],
            env=_base_env(),
            timeout=15,
            secrets=secrets,
        )
        actual_version = _redact(version.stdout, secrets).strip()
        if version.returncode != 0 or actual_version != SUPPORTED_CLI_VERSION:
            raise PostizError(
                f"Postiz CLI version {SUPPORTED_CLI_VERSION} is required; found "
                f"{actual_version or 'unknown'}",
                kind="version_mismatch",
            )
        env = _minimal_env(settings, isolated_home)
        completed = _execute(
            [*command, *argv],
            env=env,
            timeout=120,
            secrets=secrets,
        )
    stdout = _redact(completed.stdout, secrets)
    stderr = _redact(completed.stderr, secrets)
    if completed.returncode != 0:
        return {
            "success": False,
            "error_kind": "cli_failed",
            "exit_code": completed.returncode,
            "error": stderr.strip() or stdout.strip() or "Postiz CLI failed",
        }
    parsed = _extract_json(stdout)
    if parsed is not None:
        return {"success": True, "data": parsed}
    return {"success": True, "message": stdout.strip()}


def _bounded(value: str, label: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise PostizError(f"{label} must contain 1 to {limit} characters")
    return normalized


def _identifier(value: str, label: str = "id") -> str:
    normalized = value.strip()
    if not _ID_RE.fullmatch(normalized):
        raise PostizError(f"{label} is invalid")
    return normalized


def _optional_identifier(value: str, label: str) -> str:
    return _identifier(value, label) if value.strip() else ""


def _json_arg(value: Any, label: str) -> str:
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PostizError(f"{label} must be JSON-compatible") from exc
    if len(encoded) > _MAX_JSON:
        raise PostizError(f"{label} is too large")
    return encoded


def connection_status() -> dict[str, Any]:
    """Verify Postiz authentication and return the connected accounts."""
    return _run(["integrations:list"])


def integrations_list(group: str = "") -> dict[str, Any]:
    """List connected social accounts, optionally within one group."""
    argv = ["integrations:list"]
    normalized = _optional_identifier(group, "group")
    if normalized:
        argv.extend(["--group", normalized])
    return _run(argv)


def groups_list() -> dict[str, Any]:
    """List Postiz groups (customers)."""
    return _run(["integrations:groups"])


def integration_settings(integration_id: str) -> dict[str, Any]:
    """Get the posting schema and platform tools for an integration."""
    return _run(["integrations:settings", _identifier(integration_id, "integration_id")])


def integration_trigger(
    integration_id: str,
    method: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a platform discovery helper such as listing flairs or playlists."""
    normalized_method = method.strip()
    if not _METHOD_RE.fullmatch(normalized_method):
        raise PostizError("method is invalid")
    return _run([
        "integrations:trigger",
        _identifier(integration_id, "integration_id"),
        normalized_method,
        "--data",
        _json_arg(data or {}, "data"),
    ])


def posts_create(
    blocks: list[PostBlock],
    integrations: list[str],
    date: str,
    post_type: str = "schedule",
    delay: int = 0,
    short_link: bool = True,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a draft or scheduled post; blocks preserve thread/comment order."""
    if not 1 <= len(blocks) <= 100:
        raise PostizError("blocks must contain 1 to 100 items")
    if not 1 <= len(integrations) <= 50:
        raise PostizError("integrations must contain 1 to 50 ids")
    normalized_integrations = [_identifier(item, "integration") for item in integrations]
    if len(set(normalized_integrations)) != len(normalized_integrations):
        raise PostizError("integrations must be unique")
    if post_type not in {"draft", "schedule"}:
        raise PostizError("post_type must be draft or schedule")
    if not 0 <= delay <= 43_200:
        raise PostizError("delay must be between 0 and 43200 minutes")
    normalized_date = _bounded(date, "date", 64)
    timestamp_pattern = (
        r"\d{4}-\d\d-\d\dT\d\d:\d\d"
        r"(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)"
    )
    if not re.fullmatch(timestamp_pattern, normalized_date):
        raise PostizError("date must be an ISO 8601 timestamp with timezone")
    argv = ["posts:create"]
    for index, block in enumerate(blocks):
        content = block.get("content")
        media = block.get("media", [])
        if not isinstance(content, str) or not isinstance(media, list):
            raise PostizError(f"blocks[{index}] requires string content and media list")
        argv.extend([
            "--content",
            _bounded(content, f"blocks[{index}].content", _MAX_CONTENT),
        ])
        if len(media) > 20 or not all(isinstance(item, str) for item in media):
            raise PostizError(f"blocks[{index}].media must contain at most 20 strings")
        normalized_media = [
            _bounded(item, f"blocks[{index}].media", 2048)
            for item in media
        ]
        argv.extend(["--media", ",".join(normalized_media)])
    argv.extend([
        "--integrations",
        ",".join(normalized_integrations),
        "--date",
        normalized_date,
        "--type",
        post_type,
        "--delay",
        str(delay),
        "--shortLink" if short_link else "--no-shortLink",
    ])
    if settings is not None:
        argv.extend(["--settings", _json_arg(settings, "settings")])
    return _run(argv)


def posts_list(start_date: str = "", end_date: str = "", customer: str = "") -> dict[str, Any]:
    """List posts in an optional date range or customer group."""
    argv = ["posts:list"]
    for flag, value, label in (
        ("--startDate", start_date, "start_date"),
        ("--endDate", end_date, "end_date"),
    ):
        if value.strip():
            argv.extend([flag, _bounded(value, label, 64)])
    normalized_customer = _optional_identifier(customer, "customer")
    if normalized_customer:
        argv.extend(["--customer", normalized_customer])
    return _run(argv)


def posts_delete(post_id: str) -> dict[str, Any]:
    """Permanently delete one Postiz post."""
    return _run(["posts:delete", _identifier(post_id, "post_id")])


def posts_status(post_id: str, status: str) -> dict[str, Any]:
    """Move a post to draft or schedule; draft stops its active workflow."""
    if status not in {"draft", "schedule"}:
        raise PostizError("status must be draft or schedule")
    return _run(["posts:status", _identifier(post_id, "post_id"), "--status", status])


def posts_missing(post_id: str) -> dict[str, Any]:
    """List provider content that can resolve a missing release ID."""
    return _run(["posts:missing", _identifier(post_id, "post_id")])


def posts_connect(post_id: str, release_id: str) -> dict[str, Any]:
    """Connect a published Postiz post to its platform release ID."""
    return _run([
        "posts:connect",
        _identifier(post_id, "post_id"),
        "--release-id",
        _identifier(release_id, "release_id"),
    ])


def analytics_platform(integration_id: str, days: int = 7) -> dict[str, Any]:
    """Get analytics for a connected platform account."""
    if not 1 <= days <= 3650:
        raise PostizError("days must be between 1 and 3650")
    return _run([
        "analytics:platform",
        _identifier(integration_id, "integration_id"),
        "--date",
        str(days),
    ])


def analytics_post(post_id: str, days: int = 7) -> dict[str, Any]:
    """Get analytics for a published post."""
    if not 1 <= days <= 3650:
        raise PostizError("days must be between 1 and 3650")
    return _run([
        "analytics:post",
        _identifier(post_id, "post_id"),
        "--date",
        str(days),
    ])


def upload(file_path: str) -> dict[str, Any]:
    """Upload a supported media file located inside the active session cwd."""
    cwd = (
        os.environ.get("BETTER_AGENT_CWD")
        or os.environ.get("BETTER_CLAUDE_CWD")
        or os.getcwd()
    )
    root = Path(cwd).resolve()
    candidate = Path(file_path).expanduser()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise PostizError("file_path must resolve to a regular file inside the session cwd")
    if resolved.suffix.lower() not in _MEDIA_SUFFIXES:
        raise PostizError("file_path must be PNG, JPG, JPEG, GIF, or MP4")
    if resolved.stat().st_size > 2_000_000_000:
        raise PostizError("file_path exceeds the 2 GB upload limit")
    return _run(["upload", resolved.as_posix()])


def _specs() -> tuple[OperationSpec, ...]:
    mutation = {"sensitive": True}
    return (
        OperationSpec("connection_status", connection_status),
        OperationSpec("integrations_list", integrations_list),
        OperationSpec("groups_list", groups_list),
        OperationSpec("integration_settings", integration_settings),
        OperationSpec("integration_trigger", integration_trigger),
        OperationSpec("posts_create", posts_create, **mutation),
        OperationSpec("posts_list", posts_list),
        OperationSpec("posts_delete", posts_delete, **mutation),
        OperationSpec("posts_status", posts_status, **mutation),
        OperationSpec("posts_missing", posts_missing),
        OperationSpec("posts_connect", posts_connect, **mutation),
        OperationSpec("analytics_platform", analytics_platform),
        OperationSpec("analytics_post", analytics_post),
        OperationSpec("upload", upload, **mutation),
    )


def build_server():
    return build_mcp_server("postiz", _specs(), local=True)


def main() -> int:
    if sys.argv[1:2] == ["cli"]:
        build_cli_app("postiz", _specs(), local=True)(args=sys.argv[2:])
        return 0
    if sys.argv[1:]:
        raise SystemExit("expected no arguments for MCP mode or 'cli' for CLI mode")
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
