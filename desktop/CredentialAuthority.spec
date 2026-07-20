# -*- mode: python ; coding: utf-8 -*-

import os

_HERE = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821
_REPO = os.path.dirname(_HERE)
_BACKEND = os.path.join(_REPO, "backend")
_DESKTOP = os.path.join(_REPO, "desktop")

hiddenimports = [
    "daemonhost",
    "daemonhost.jsonio",
    "daemonhost.paths",
    "daemonhost.pointer",
    "keyring.backends.macOS.api",
]

analysis = Analysis(  # noqa: F821
    [os.path.join(_DESKTOP, "credential_supervisor_main.py")],
    pathex=[_REPO, _BACKEND, _DESKTOP],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(analysis.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="BetterAgentCredentialAuthority",
    console=True,
    target_arch=None,
    codesign_identity=None,
)
