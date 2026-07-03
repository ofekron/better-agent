"""First-run credential setup for the Better Agent desktop app.

The backend cannot boot until the OS credential store holds the three
`better-claude` entries (`SessionMiddleware` reads the session secret at
import time; `auth_secrets` maps to the macOS Keychain on darwin and the
Windows Credential Manager elsewhere via `keyring`). On a fresh install
the shell collects a username + password and writes them via
`auth_secrets.write_credentials`.

Collected BEFORE the GUI event loop starts — pywebview's `webview.start()`
runs once per process, so setup cannot be its own pywebview window. Native
`osascript` dialogs on macOS; `tkinter` dialogs on Windows/Linux.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Literal, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import auth_secrets
from env_compat import dual_env, get_env

_TITLE = "Better Agent Setup"
DesktopRole = Literal["primary", "node"]
PrimaryBindChoice = Literal["127.0.0.1", "0.0.0.0"]
PortConflictChoice = Literal["kill", "use_port"]


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _parse_dialog_output(stdout: str) -> str:
    """Extract the entered text from `osascript display dialog` output,
    which is `button returned:OK, text returned:<value>`."""
    marker = "text returned:"
    idx = stdout.find(marker)
    return stdout[idx + len(marker):].strip() if idx != -1 else ""


def _parse_dialog_button(stdout: str) -> str:
    marker = "button returned:"
    idx = stdout.find(marker)
    if idx == -1:
        return ""
    rest = stdout[idx + len(marker):]
    return rest.split(",", 1)[0].strip()


def _escape_applescript_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prompt_macos(
    message: str, *, hidden: bool, default: str = "",
) -> Optional[str]:
    script = (
        f'display dialog "{_escape_applescript_text(message)}" '
        f'default answer "{_escape_applescript_text(default)}" '
        + ("with hidden answer " if hidden else "")
        + f'buttons {{"Cancel", "OK"}} default button "OK" with title "{_TITLE}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return _parse_dialog_output(result.stdout)


def _choose_role_macos() -> Optional[DesktopRole]:
    script = (
        'display dialog "How should this machine start Better Agent?" '
        'buttons {"Cancel", "Join as Node", "Host Primary"} '
        'default button "Host Primary" '
        f'with title "{_TITLE}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    button = _parse_dialog_button(result.stdout)
    if button == "Host Primary":
        return "primary"
    if button == "Join as Node":
        return "node"
    return None


def _choose_primary_bind_macos() -> Optional[PrimaryBindChoice]:
    script = (
        'display dialog "How should the primary backend be reachable?" '
        'buttons {"Cancel", "Local Network", "This Mac Only"} '
        'default button "This Mac Only" '
        f'with title "{_TITLE}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    button = _parse_dialog_button(result.stdout)
    if button == "This Mac Only":
        return "127.0.0.1"
    if button == "Local Network":
        return "0.0.0.0"
    return None


def _choose_port_conflict_macos(
    port: int, listener_text: str,
) -> Optional[PortConflictChoice]:
    escaped_listener_text = _escape_applescript_text(listener_text)
    script = (
        f'display dialog "Port {port} is already in use by:\\n'
        f'{escaped_listener_text}\\n\\nWhat should Better Agent do?" '
        'buttons {"Cancel", "Use Different Port", "Kill Process"} '
        'default button "Use Different Port" '
        f'with title "{_TITLE}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    button = _parse_dialog_button(result.stdout)
    if button == "Kill Process":
        return "kill"
    if button == "Use Different Port":
        return "use_port"
    return None


def _alert_macos(message: str) -> None:
    subprocess.run(
        ["osascript", "-e",
         f'display alert "{_TITLE}" message "{message}"'],
        capture_output=True,
    )


def _prompt_tk(
    message: str, *, hidden: bool, default: str = "",
) -> Optional[str]:
    """tkinter input dialog. Returns the entered text, or None on Cancel
    (`askstring` returns None when dismissed) — matching the osascript
    contract."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        return simpledialog.askstring(
            _TITLE,
            message,
            show="*" if hidden else "",
            initialvalue=default,
            parent=root,
        )
    finally:
        root.destroy()


def _choose_role_tk() -> Optional[DesktopRole]:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        result = messagebox.askyesnocancel(
            _TITLE,
            "How should this machine start Better Agent?\n\n"
            "Yes: Host Primary\nNo: Join as Node",
            parent=root,
        )
    finally:
        root.destroy()
    if result is True:
        return "primary"
    if result is False:
        return "node"
    return None


def _choose_primary_bind_tk() -> Optional[PrimaryBindChoice]:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        result = messagebox.askyesnocancel(
            _TITLE,
            "How should the primary backend be reachable?\n\n"
            "Yes: This Mac only\nNo: Local network",
            parent=root,
        )
    finally:
        root.destroy()
    if result is True:
        return "127.0.0.1"
    if result is False:
        return "0.0.0.0"
    return None


def _choose_port_conflict_tk(
    port: int, listener_text: str,
) -> Optional[PortConflictChoice]:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        result = messagebox.askyesnocancel(
            _TITLE,
            f"Port {port} is already in use by:\n{listener_text}\n\n"
            "Yes: Kill process\nNo: Use different port",
            parent=root,
        )
    finally:
        root.destroy()
    if result is True:
        return "kill"
    if result is False:
        return "use_port"
    return None


def _alert_tk(message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showwarning(_TITLE, message, parent=root)
    finally:
        root.destroy()


def _prompt(
    message: str, *, hidden: bool = False, default: str = "",
) -> Optional[str]:
    """Show one input dialog. Returns the entered text, or None if the
    user cancelled."""
    impl = _prompt_macos if _is_macos() else _prompt_tk
    return impl(message, hidden=hidden, default=default)


def _choose_role() -> Optional[DesktopRole]:
    impl = _choose_role_macos if _is_macos() else _choose_role_tk
    return impl()


def _choose_primary_bind() -> Optional[PrimaryBindChoice]:
    impl = _choose_primary_bind_macos if _is_macos() else _choose_primary_bind_tk
    return impl()


def _choose_port_conflict(
    port: int, listener_text: str,
) -> Optional[PortConflictChoice]:
    impl = (
        _choose_port_conflict_macos
        if _is_macos()
        else _choose_port_conflict_tk
    )
    return impl(port, listener_text)


def _alert(message: str) -> None:
    impl = _alert_macos if _is_macos() else _alert_tk
    impl(message)


def _desktop_role_path() -> Path:
    from paths import ba_home
    return ba_home() / "desktop_role"


def _primary_bind_configured_path() -> Path:
    from paths import ba_home
    return ba_home() / "desktop_primary_bind_configured"


def _topology_path() -> Path:
    import os
    from paths import ba_home
    return Path(
        get_env("BETTER_CLAUDE_TOPOLOGY_PATH")
        or str(ba_home() / "topology.yaml")
    ).expanduser()


def _read_desktop_role() -> Optional[DesktopRole]:
    path = _desktop_role_path()
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if value in ("primary", "node"):
        return value
    raise RuntimeError(f"Invalid desktop role stored at {path}: {value!r}")


def _write_desktop_role(role: DesktopRole) -> None:
    path = _desktop_role_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(role + "\n", encoding="utf-8")


def ensure_desktop_role() -> Optional[DesktopRole]:
    """Return this desktop install's role, asking once on first launch."""
    role = _read_desktop_role()
    if role is not None:
        return role
    role = _choose_role()
    if role is None:
        return None
    _write_desktop_role(role)
    return role


def ensure_primary_network_bind() -> bool:
    """Ask once whether primary should bind localhost or the LAN."""
    path = _primary_bind_configured_path()
    if path.exists():
        return True
    address = _choose_primary_bind()
    if address is None:
        return False
    import user_prefs
    user_prefs.set_network_bind_address(address)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(address + "\n", encoding="utf-8")
    return True


def _normalize_primary_address(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("Primary address is required.")
    if not value.startswith(("ws://", "wss://", "http://", "https://")):
        value = f"ws://{value}"
    if value.startswith("http://"):
        value = "ws://" + value[len("http://"):]
    elif value.startswith("https://"):
        value = "wss://" + value[len("https://"):]
    without_scheme = value.split("://", 1)[1]
    host_port = without_scheme.split("/", 1)[0]
    if ":" not in host_port:
        value = value.rstrip("/") + ":8000"
    return value


def _primary_http_url(primary_address: str) -> str:
    if primary_address.startswith("ws://"):
        return "http://" + primary_address[len("ws://"):].rstrip("/")
    if primary_address.startswith("wss://"):
        return "https://" + primary_address[len("wss://"):].rstrip("/")
    return primary_address.rstrip("/")


def _post_json(url: str, payload: dict, *, token: str = "") -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"detail": raw}
        return exc.code, body


def _get_json(url: str, *, token: str = "") -> tuple[int, dict]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urlrequest.Request(url, headers=headers, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"detail": raw}
        return exc.code, body


def _verify_primary_for_node(primary_address: str) -> bool:
    """Login to the primary and require Machine nodes to be ready there."""
    base = _primary_http_url(primary_address)
    username = _prompt("Primary username:")
    if username is None:
        return False
    password = _prompt("Primary password:", hidden=True)
    if password is None:
        return False
    try:
        status, body = _post_json(
            f"{base}/api/auth/login",
            {"username": username, "password": password},
        )
        if status != 200 or not body.get("token"):
            _alert("Could not log in to the primary. Check the username and password.")
            return False
        status, body = _get_json(f"{base}/api/nodes", token=str(body["token"]))
    except (OSError, TimeoutError, ValueError) as exc:
        _alert(f"Could not reach the primary at {base}: {exc}")
        return False
    if status == 200:
        return True
    if status == 404:
        _alert(
            "The primary is reachable, but Machine nodes is not installed "
            "or enabled there. Open the primary Better Agent app, install "
            "and enable the Machine nodes extension, then run this setup again."
        )
        return False
    if status in (401, 403):
        _alert("The primary rejected the login. Check the username and password.")
        return False
    detail = body.get("detail") or body
    _alert(f"Could not verify Machine nodes on the primary: HTTP {status}: {detail}")
    return False


def _write_node_topology(primary_address: str) -> None:
    path = _topology_path()
    if not path.is_absolute():
        raise RuntimeError(
            f"BETTER_AGENT_TOPOLOGY_PATH or BETTER_CLAUDE_TOPOLOGY_PATH must be absolute, got {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "schema_version: 1\n"
        "primary:\n"
        "  id: primary\n"
        f"  address: {primary_address}\n"
        "  cwd_roots: []\n"
        "nodes: {}\n"
    )
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def ensure_node_topology() -> bool:
    """Ensure node mode has the primary address topology it needs."""
    import os
    path = _topology_path()
    os.environ.update(dual_env("BETTER_CLAUDE_TOPOLOGY_PATH", str(path)))
    if path.exists():
        return True
    raw = _prompt(
        "Enter the primary machine address or IP "
        "(port 8000 is used when omitted):"
    )
    if raw is None:
        return False
    try:
        primary_address = _normalize_primary_address(raw)
        if not _verify_primary_for_node(primary_address):
            return False
        _write_node_topology(primary_address)
    except Exception as e:  # noqa: BLE001
        _alert(str(e))
        return False
    return True


def _listener_text(listeners: list[dict]) -> str:
    if not listeners:
        return "Unknown listener"
    return "\n".join(
        f"{listener.get('command', 'unknown')} (PID {listener.get('pid')})"
        for listener in listeners
    )


def _parse_port(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        port = int(raw.strip())
    except ValueError:
        _alert("Port must be a number.")
        return None
    if port < 1 or port > 65535:
        _alert("Port must be between 1 and 65535.")
        return None
    return port


def resolve_port_conflict(port: int, listeners: list[dict]) -> Optional[dict]:
    choice = _choose_port_conflict(port, _listener_text(listeners))
    if choice is None:
        return None
    if choice == "kill":
        return {"action": "kill", "port": port}
    new_port = _parse_port(_prompt("Enter a different port:"))
    if new_port is None:
        return None
    return {"action": "use_port", "port": new_port}


def run_setup() -> bool:
    """Collect a username + password via native dialogs and write them to
    the keychain. Returns True once credentials are stored, False if the
    user cancelled."""
    username = _prompt(
        "Welcome to Better Agent — choose a username "
        "(used here and from other devices on your network):"
    )
    if username is None:
        return False
    username = username.strip()
    if not username:
        _alert("Username cannot be empty.")
        return False
    while True:
        password = _prompt("Choose a password:", hidden=True)
        if password is None:
            return False
        if not password:
            _alert("Password cannot be empty. Try again.")
            continue
        confirm = _prompt("Confirm the password:", hidden=True)
        if confirm is None:
            return False
        if password != confirm:
            _alert("The passwords did not match. Try again.")
            continue
        break
    try:
        auth_secrets.write_credentials(username, password)
    except Exception as e:  # noqa: BLE001 — surface any failure to the user
        _alert(f"Could not save credentials: {e}")
        return False
    return True
