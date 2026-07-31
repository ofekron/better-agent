from __future__ import annotations

import os
import platform
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import config_store


def config_root(root: Path, value: str = "") -> Path:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(value, encoding="utf-8")
    return config


def provider(
    root: Path,
    *,
    kind: str = "codex",
    runner: str = "native",
    config_dir: Path | None = None,
    use_ambient_config: bool = False,
) -> dict:
    selected_config = (
        ""
        if use_ambient_config
        else str(config_dir or config_root(root))
    )
    return config_store.add_provider({
        "name": f"Discovery {kind}",
        "kind": kind,
        "mode": "subscription",
        "runner": runner,
        "config_dir": selected_config,
    })


def write_executable(
    path: Path,
    models: object,
    *,
    mutate: str = "",
    provider_drift: tuple[str, str, int] | None = None,
    delay_seconds: float = 0,
    exit_code: int = 0,
    expected_args: tuple[str, ...] | None = None,
    pid_file: Path | None = None,
    invocation_log: Path | None = None,
    raw_output_bytes: int = 0,
    forbidden_environment_value: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    drift = _provider_drift_source(provider_drift)
    mutation = _mutation_source(mutate)
    output = (
        f"sys.stdout.write('x' * {raw_output_bytes})\n"
        if raw_output_bytes
        else f"print(json.dumps({models!r}))\n"
    )
    source = (
        "#!/usr/bin/python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        + (
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            if pid_file is not None
            else ""
        )
        + (
            f"with Path({str(invocation_log)!r}).open('a', encoding='utf-8') as stream:\n"
            "    stream.write('invoked\\n')\n"
            if invocation_log is not None
            else ""
        )
        + (
            f"if tuple(sys.argv[1:]) != {expected_args!r}:\n"
            "    raise SystemExit(64)\n"
            if expected_args is not None
            else ""
        )
        + (
            f"if {forbidden_environment_value!r} in os.environ.values():\n"
            "    raise SystemExit(65)\n"
            if forbidden_environment_value
            else ""
        )
        + drift
        + mutation
        + f"time.sleep({delay_seconds!r})\n"
        + f"if {exit_code}:\n"
        + f"    raise SystemExit({exit_code})\n"
        + output
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _provider_drift_source(
    authority: tuple[str, str, int] | None,
) -> str:
    if authority is None:
        return ""
    provider_id, generation, revision = authority
    state_home = os.environ["BETTER_AGENT_HOME"]
    state_path = str(Path(state_home) / "config.json")
    return (
        "import hashlib\n"
        f"state_path = Path({state_path!r})\n"
        "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
        f"record = next(p for p in state['providers'] if p['id'] == {provider_id!r})\n"
        f"assert record['generation'] == {generation!r}\n"
        f"assert record['revision'] == {revision}\n"
        "record['nickname'] = 'drifted'\n"
        "record['revision'] += 1\n"
        "digest_payload = {\n"
        "    'default_provider_id': state['default_provider_id'],\n"
        "    'providers': state['providers'],\n"
        "}\n"
        "encoded = json.dumps(digest_payload, ensure_ascii=False, "
        "separators=(',', ':'), sort_keys=True).encode('utf-8')\n"
        "state['provider_state_authority']['revision'] += 1\n"
        "state['provider_state_authority']['digest'] = "
        "hashlib.sha256(encoded).hexdigest()\n"
        "replacement = state_path.with_suffix('.replacement')\n"
        "replacement.write_text(json.dumps(state), encoding='utf-8')\n"
        "os.replace(replacement, state_path)\n"
    )


def _mutation_source(mutation: str) -> str:
    if mutation == "config":
        return (
            "config = Path(os.environ['CODEX_HOME']) / 'config.toml'\n"
            "config.write_text('changed = true\\n', encoding='utf-8')\n"
        )
    if mutation == "config_transient":
        return (
            "config = Path(os.environ['CODEX_HOME']) / 'config.toml'\n"
            "config.write_text('model = \"transient\"\\n', encoding='utf-8')\n"
            "config.unlink()\n"
        )
    if mutation == "stabilize_then_delay":
        return (
            "marker = Path(os.environ['CODEX_HOME']) / 'tmp'\n"
            "if marker.exists():\n"
            "    time.sleep(5)\n"
            "else:\n"
            "    marker.mkdir()\n"
            "    time.sleep(0.1)\n"
        )
    if mutation == "executable":
        return (
            "with Path(__file__).open('a', encoding='utf-8') as stream:\n"
            "    stream.write('\\n# changed\\n')\n"
        )
    return ""


@contextmanager
def codex_on_path(executable: Path):
    bin_dir = executable.parent
    logical = bin_dir / "codex"
    if logical != executable:
        logical.symlink_to(executable)
    previous = os.environ.get("PATH")
    path_entries = [str(bin_dir), str(Path(sys.prefix) / "bin")]
    if previous:
        path_entries.append(previous)
    os.environ["PATH"] = os.pathsep.join(path_entries)
    try:
        yield logical
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


@contextmanager
def environment(name: str, value: str):
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def package_name() -> str:
    architecture = platform.machine().lower()
    if architecture in {"amd64", "x86_64"}:
        architecture = "x64"
    elif architecture == "aarch64":
        architecture = "arm64"
    if sys.platform.startswith("darwin"):
        return f"codex-darwin-{architecture}"
    if sys.platform.startswith("linux"):
        return f"codex-linux-{architecture}"
    return f"codex-win32-{architecture}"


def visible_catalog() -> dict:
    return {
        "models": [
            {"slug": " zeta ", "visibility": "list", "priority": 4},
            {"slug": "alpha", "visibility": "list", "priority": 1},
            {"slug": "zeta", "visibility": "list", "priority": 2},
            {"slug": "hidden", "visibility": "hide", "priority": 0},
        ],
    }
