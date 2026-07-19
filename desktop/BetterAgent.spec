# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Better Agent macOS desktop app.

Produces `Better Agent.app` — ONE binary, three roles (shell / server /
runner) dispatched by `desktop/app_main.py`.

Build:  cd desktop && pyinstaller BetterAgent.spec

NOTE: the `hiddenimports` / `datas` lists below are a STARTING POINT. A
first build will surface modules or data files PyInstaller's static
analysis missed (dynamic imports, package data) — add them here and
rebuild. This is normal PyInstaller iteration, not a defect.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

_HERE = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821 (SPEC: PyInstaller global)
_REPO = os.path.dirname(_HERE)
_BACKEND = os.path.join(_REPO, "backend")
_DESKTOP = os.path.join(_REPO, "desktop")

# Single source of truth for the app version (desktop/_version.py).
sys.path.insert(0, _DESKTOP)
from _version import __version__ as _APP_VERSION  # noqa: E402

datas = [
    # The built frontend — served by the backend; resolved at runtime via
    # `sys._MEIPASS / "frontend_dist"` (see backend/main.py).
    (os.path.join(_REPO, "frontend", "dist"), "frontend_dist"),
    (os.path.join(_BACKEND, "prompts"), "prompts"),
    (os.path.join(_BACKEND, "provisioning", "prompts"), os.path.join("prompts", "provisioning")),
]
_TUFUP_ROOT = os.path.join(_DESKTOP, "tufup_root.json")
if os.path.exists(_TUFUP_ROOT):
    datas.append((_TUFUP_ROOT, "."))
_UPDATE_URL = os.path.join(_DESKTOP, "update_url.txt")
if os.path.exists(_UPDATE_URL):
    datas.append((_UPDATE_URL, "."))
binaries = []
hiddenimports = [
    # Runner modules are loaded via importlib in app_entry (dynamic — not
    # visible to static analysis), so every provider_manifest runner_module
    # must be listed here explicitly.
    "main", "main_node", "app_entry", "runner", "runner_gemini",
    "runner_codex", "runner_better_agent", "runner_agy", "runner_copilot",
    "shell", "supervisor", "shell_env", "setup", "auth_secrets",
    "keyring.backends.macOS.api",
    "updater", "_version",
    "node_client", "node_identity", "node_link", "node_protocol",
    "node_registry_store", "node_rpc_handlers", "node_store", "topology",
    # `i18n` is a Python package (`from i18n import t`) — listed so it
    # enters the frozen module graph; it must NOT be bundled as `datas`
    # (raw file copies are never `import`-able).
    "i18n",
]

# These packages ship data files and/or use dynamic imports that
# PyInstaller's static analysis does not fully follow.
def _without_python_sources(_datas):
    return [
        (_src, _dest)
        for _src, _dest in _datas
        if not _src.endswith(".py")
    ]


for _pkg in ("claude_agent_sdk", "argon2", "uvicorn", "fastapi",
             "starlette", "webview", "tufup"):
    _d, _b, _h = collect_all(_pkg)
    datas += _without_python_sources(_d)
    binaries += _b
    hiddenimports += _h

hiddenimports += [
    "daemonhost",
    "daemonhost.host",
    "daemonhost.install",
    "daemonhost.jsonio",
    "daemonhost.paths",
    "daemonhost.pointer",
    "daemonhost.switch_control",
    "switch_control_daemon",
    "switch_control_daemon.line_switch_runtime",
    "switch_control_daemon.line_switch_runtime.control",
    "switch_control_daemon.line_switch_runtime.jsonio",
    "switch_control_daemon.line_switch_runtime.paths",
    "switch_control_daemon.line_switch_runtime.pointer",
    "switch_control_daemon.line_switch_runtime.requests",
    "switch_control_daemon.line_switch_runtime.service",
    "switch_control_daemon.line_switch_runtime.transaction",
]

a = Analysis(                                            # noqa: F821
    [os.path.join(_DESKTOP, "app_main.py")],
    pathex=[_BACKEND, _DESKTOP, _REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)                                        # noqa: F821

exe = EXE(                                               # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Better Agent",
    console=False,          # windowed app — no terminal
    target_arch=None,       # build for the host architecture
    codesign_identity=None,  # ad-hoc signed by build_macos.sh
)
coll = COLLECT(                                          # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="Better Agent",
)
# macOS ships a `.app` bundle (Info.plist + App Transport Security). On
# Windows/Linux the COLLECT onedir above IS the distributable; the
# platform build script (build_windows.ps1) wraps it in an installer.
if sys.platform == "darwin":
    app = BUNDLE(                                        # noqa: F821
        coll,
        name="Better Agent.app",
        icon=None,
        bundle_identifier="com.betteragent.app",
        info_plist={
            "CFBundleName": "Better Agent",
            "CFBundleDisplayName": "Better Agent",
            "CFBundleShortVersionString": _APP_VERSION,
            "CFBundleVersion": _APP_VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # The backend binds 0.0.0.0 for LAN access; macOS 15+ prompts
            # for local-network permission and wants a usage string.
            "NSLocalNetworkUsageDescription":
                "Better Agent serves its UI to other devices on your "
                "network.",
            # The WebView loads `http://127.0.0.1:8000/` (plain HTTP).
            # macOS App Transport Security blocks plaintext loads from a
            # bundled app unless this exception declares local-networking
            # is allowed. Loopback / link-local only — NOT arbitrary HTTP.
            "NSAppTransportSecurity": {
                "NSAllowsLocalNetworking": True,
            },
        },
    )
