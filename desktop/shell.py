"""Better Agent desktop shell — native macOS window + backend supervisor.

This is the macOS app's main process. It supervises the FastAPI backend
as a child process, shows the backend-served UI in a native window,
respawns the backend on `/api/admin/restart`, and on close asks whether
to kill the running Claude/Gemini runner subprocesses.

GUI-independent logic lives in `supervisor.py` / `shell_env.py` (unit
tested); this module is the thin pywebview wiring.
"""

from __future__ import annotations

import html
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import webview

import updater
from notifications import DesktopNotificationApi
from supervisor import BackendSupervisor
from updater import start_background_check


def _watch_for_restart(
    sup: BackendSupervisor, window, quitting: threading.Event, local_url: str,
) -> None:
    """Background thread. When the backend process exits:
      - user-initiated quit (`quitting` set) → do nothing; `main` tears down.
      - `/api/admin/restart` → respawn the backend and reload the window.
      - anything else (a crash) → close the window so the app exits.
    """
    while True:
        sup.wait_exit()
        if quitting.is_set():
            return
        if sup.restart_was_requested() and sup.restart():
            window.load_url(local_url)
            continue
        window.destroy()
        return


def _error_window(message: str) -> None:
    if platform.system() == "Darwin":
        _alert_macos("Better Agent could not start", message)
        return
    safe_message = html.escape(message)
    webview.create_window(
        "Better Agent",
        html=(
            "<body style='font:14px -apple-system,sans-serif;padding:2em'>"
            "<h2>Better Agent could not start</h2>"
            f"<p>{safe_message}</p></body>"
        ),
    )
    webview.start()


def _line_switch_fallback() -> bool:
    try:
        switch_home = Path(os.environ.get("BA_SWITCH_HOME", "~/.ba-switch")).expanduser()
        config = json.loads((switch_home / "web.json").read_text(encoding="utf-8"))
        if not isinstance(config.get("port"), int) or not isinstance(config.get("token"), str):
            return False
        url = f"http://127.0.0.1:{config['port']}/#{config['token']}"
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url.split("#", 1)[0], timeout=0.5) as response:
                if response.status == 200:
                    webview.create_window("Better Agent — Line Switch", url)
                    webview.start()
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    return False


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _alert_macos(title: str, message: str) -> None:
    script = (
        "display alert "
        f"{_applescript_string(title)} "
        f"message {_applescript_string(message)} "
        'buttons {"OK"} default button "OK"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _configure_logging() -> None:
    """Route the shell's own logging to `ba_home()/shell.log` with size-
    based rotation (~50 MB across 5 files). A Finder-launched .app has
    no terminal — without this every shell `logger.*` call vanishes.
    Combined with the rotating `backend.log` (~450 MB) total log disk
    usage is capped at ~0.5 GB. `supervisor` already put `backend/` on
    `sys.path`, so `paths` is importable."""
    import logging
    import logging.handlers
    from paths import ba_home
    log_path = ba_home() / "shell.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=10 * 1024 * 1024,   # 10 MB per file
        backupCount=4,                # 5 files × 10 MB = 50 MB
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)


def main() -> int:
    _configure_logging()
    from setup import (
        ensure_desktop_role,
        ensure_node_topology,
        ensure_primary_network_bind,
        resolve_port_conflict,
    )
    role = ensure_desktop_role()
    if role is None:
        return 1
    if role == "node":
        if not ensure_node_topology():
            return 1
    else:
        if not ensure_primary_network_bind():
            return 1
        # Primary first run: the backend cannot boot until the keychain
        # holds the credential entries (SessionMiddleware reads the secret
        # at import time), so collect them before starting the backend.
        import auth_secrets
        if auth_secrets.needs_bootstrap():
            from setup import run_setup
            if not run_setup():
                return 1  # user closed setup without creating an account

    sup = BackendSupervisor(role=role)
    try:
        sup.start(on_port_conflict=resolve_port_conflict)
    except RuntimeError as e:
        _error_window(str(e))
        return 1
    if not sup.wait_healthy():
        message = (
            f"The backend did not come up on port {sup.port} "
            f"(it may have failed during startup — check the logs)."
        )
        if not _line_switch_fallback():
            _error_window(message)
        sup.shutdown(kill_runners=True)
        return 1

    local_url = sup.local_url()
    quitting = threading.Event()
    kill_on_quit = {"value": False}
    if role == "node":
        window = webview.create_window(
            "Better Agent Node",
            html=(
                "<body style='font:14px -apple-system,sans-serif;padding:2em'>"
                "<h2>Better Agent node is running</h2>"
                "<p>This machine is waiting for the primary machine to approve "
                "or use it as a node.</p>"
                f"<p>Local node health: <code>{sup.health_url}</code></p>"
                "</body>"
            ),
        )
    else:
        window = webview.create_window(
            "Better Agent",
            local_url,
            js_api=DesktopNotificationApi(),
        )

    def _on_closing() -> None:
        # Runs on the GUI thread when the window is closing. The confirm
        # dialog is quick user interaction; the possibly-slow backend stop
        # is deferred until after `webview.start()` returns so the GUI
        # thread is never blocked by it. Returning None lets the window
        # close. `quitting` is set first so `_watch_for_restart` does not
        # mistake the imminent backend exit for a crash or a restart.
        quitting.set()
        kill_on_quit["value"] = bool(window.create_confirmation_dialog(
            "Quit Better Agent",
            "Also stop the running Claude/Gemini processes?\n"
            "Cancel leaves them running — they finish on their own.",
        ))

    window.events.closing += _on_closing
    threading.Thread(
        target=_watch_for_restart,
        args=(sup, window, quitting, local_url),
        daemon=True,
    ).start()

    def _on_update_available(version: str) -> None:
        # Runs on the updater's background thread. Prompt the user; on
        # accept, stop the backend (leaving detached runners alive, as a
        # restart does), then apply the update and relaunch. `quitting`
        # is set first so `_watch_for_restart` treats the backend exit as
        # an intentional teardown, not a crash to respawn.
        if not window.create_confirmation_dialog(
            "Update available",
            f"Better Agent {version} is available. "
            "Restart to update now?",
        ):
            return
        quitting.set()
        sup.shutdown(kill_runners=False)
        updater.apply_and_relaunch()

    start_background_check(_on_update_available)
    webview.start()  # blocks until the window is closed
    sup.shutdown(kill_runners=kill_on_quit["value"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
