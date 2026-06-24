"""Ensure the provider-config-sync submodule's live src is importable.

The package is also pip-installed into the backend venv, but that copy can lag
the in-repo submodule. Inserting the submodule src at the front of ``sys.path``
guarantees the live code is used regardless of import order. Idempotent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_on_path() -> Path:
    repo = Path(
        os.environ.get("PROVIDER_CONFIG_SYNC_REPO")
        or Path(__file__).resolve().parent.parent / "provider-config-sync"
    )
    package_src = repo / "packages" / "provider-config-sync-backend" / "src"
    if package_src.is_dir() and str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))
    return package_src
