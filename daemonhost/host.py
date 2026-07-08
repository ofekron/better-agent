"""Supervisor loop for supervisor-lifecycle extension daemons.

Consumes the backend-published desired set (``registry.json``), keeps
selftest-gated installed copies current, spawns each daemon with a scrubbed
environment, restarts on crash with capped backoff, and records every
transition in the host-owned ``state.json`` (the backend serves that file to
the UI as a read projection).

Registry entries are removed only by the backend on explicit
uninstall/disable. A daemon whose source is missing from the active checkout
keeps its installed copy running — a line switch must never uninstall the
daemon executing it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from daemonhost.install import (
    current_dir,
    install,
    install_meta,
    needs_install,
    promote_last_good,
    rollback_to_last_good,
)
from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import daemon_root, logs_root, registry_path, state_path

_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
# A daemon that stays up this long has completed a healthy cycle: its copy is
# promoted to last_good and its restart budget resets.
HEALTHY_CYCLE_SECONDS = 60.0
_STOP_GRACE_SECONDS = 30.0


def scrubbed_env(env_allowlist: list[str]) -> dict[str, str]:
    """Minimal env for daemon processes: never auth tokens or secrets."""
    env: dict[str, str] = {}
    for key in (*_BASE_ENV_KEYS, "BETTER_AGENT_HOME", "BETTER_CLAUDE_HOME", *env_allowlist):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


class _Daemon:
    def __init__(self, key: str, spec: dict[str, Any]):
        self.key = key
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.started_at = 0.0
        self.status = "pending"
        self.error = ""
        self.install_error = ""

    @property
    def root(self) -> Path:
        return daemon_root(self.spec["extension_id"], self.spec["name"])


class DaemonHost:
    """One instance per machine; run() blocks until stop() is called."""

    def __init__(self, poll_interval: float = 2.0):
        self._poll_interval = poll_interval
        self._daemons: dict[str, _Daemon] = {}
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self.reconcile_once()
                self._stop.wait(self._poll_interval)
        finally:
            self._shutdown_all()
            self._write_state()

    def reconcile_once(self) -> None:
        desired = self._read_registry()
        for key in list(self._daemons):
            if key not in desired:
                self._retire(self._daemons.pop(key))
        for key, spec in desired.items():
            daemon = self._daemons.get(key)
            if daemon is None or daemon.spec != spec:
                if daemon is not None:
                    self._stop_proc(daemon)
                daemon = _Daemon(key, spec)
                self._daemons[key] = daemon
            self._tick(daemon)
        self._write_state()

    def _read_registry(self) -> dict[str, dict[str, Any]]:
        entries = read_json(registry_path()).get("daemons")
        if not isinstance(entries, dict):
            return {}
        return {
            key: spec
            for key, spec in entries.items()
            if isinstance(spec, dict) and spec.get("lifecycle") == "supervisor"
        }

    def _tick(self, daemon: _Daemon) -> None:
        proc = daemon.proc
        if proc is not None:
            if proc.poll() is None:
                if daemon.status == "running" and time.time() - daemon.started_at >= HEALTHY_CYCLE_SECONDS:
                    if not install_meta(daemon.root).get("promoted"):
                        promote_last_good(daemon.root)
                        daemon.restarts = 0
                return
            daemon.proc = None
            daemon.error = f"exited with code {proc.returncode}"
            if not install_meta(daemon.root).get("promoted") and rollback_to_last_good(daemon.root):
                daemon.status = "rolled_back"
                daemon.restarts = 0
            policy = daemon.spec.get("restart_policy") or {}
            if daemon.restarts >= int(policy.get("max_restarts", 5)):
                daemon.status = "failed"
                return
            daemon.restarts += 1
            backoff = float(policy.get("backoff_seconds", 5)) * daemon.restarts
            daemon.status = "backoff"
            self._stop.wait(min(backoff, 120.0))
            if self._stop.is_set():
                return
        # Quiescent window (no child running): update the installed copy from
        # the active checkout's source, then spawn.
        if not self._ensure_installed(daemon):
            return
        self._spawn(daemon)

    def _ensure_installed(self, daemon: _Daemon) -> bool:
        source = Path(str(daemon.spec.get("source_root") or ""))
        env = scrubbed_env(daemon.spec.get("env_allowlist") or [])
        if source.is_dir() and needs_install(daemon.root, source):
            ok, error = install(daemon.root, source, daemon.spec["module"], sys.executable, env)
            daemon.install_error = "" if ok else f"install rejected: {error}"
            if not ok and not current_dir(daemon.root).is_dir():
                daemon.status = "failed"
        if not current_dir(daemon.root).is_dir():
            daemon.status = "unavailable"
            daemon.error = daemon.error or "no installed copy and no source available"
            return False
        return True

    def _spawn(self, daemon: _Daemon) -> None:
        copy = current_dir(daemon.root)
        env = scrubbed_env(daemon.spec.get("env_allowlist") or [])
        env["PYTHONPATH"] = str(copy)
        env["BETTER_AGENT_DAEMON"] = daemon.key
        logs_root().mkdir(parents=True, exist_ok=True)
        log_path = logs_root() / f"{daemon.key.replace(':', '.')}.log"
        try:
            with open(log_path, "ab") as log:
                daemon.proc = subprocess.Popen(
                    [sys.executable, "-m", daemon.spec["module"]],
                    cwd=copy,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
        except OSError as exc:
            daemon.status = "failed"
            daemon.error = str(exc)
            return
        daemon.started_at = time.time()
        daemon.status = "running"
        daemon.error = ""

    def _stop_proc(self, daemon: _Daemon) -> None:
        proc = daemon.proc
        daemon.proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _retire(self, daemon: _Daemon) -> None:
        """Registry entry gone = explicit backend uninstall/disable."""
        self._stop_proc(daemon)

    def _shutdown_all(self) -> None:
        for daemon in self._daemons.values():
            self._stop_proc(daemon)
            daemon.status = "stopped"

    def _write_state(self) -> None:
        write_json(
            state_path(),
            {
                "updated_at": time.time(),
                "host_pid": os.getpid(),
                "daemons": {
                    key: {
                        "status": d.status,
                        "pid": d.proc.pid if d.proc and d.proc.poll() is None else None,
                        "restarts": d.restarts,
                        "error": d.error,
                        "install_error": d.install_error,
                    }
                    for key, d in self._daemons.items()
                },
            },
        )
