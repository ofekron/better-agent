"""Auto-update for the Better Agent desktop shell, via `tufup`.

GUI-independent (unit tested); `desktop/shell.py` wires the GUI prompt.

Dormant unless `BA_UPDATE_URL` is set. The background check runs once on
shell start; if a newer release is found the caller is notified so the
GUI can prompt "restart to update". Applying the update replaces the
installed bundle and relaunches.

Update source layout:
    <BA_UPDATE_URL>/metadata/   TUF metadata (root/targets/snapshot/timestamp)
    <BA_UPDATE_URL>/targets/    the release archives

Use a localhost URL for local testing; a public URL for distribution.
A localhost URL only serves the machine running it — it cannot deliver
updates to other users.

Failure policy: the update CHECK is non-fatal by design — any error
(offline, host down, bad metadata) is logged and the app launches
normally. Applying an update is NOT wrapped: a failed apply propagates.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# tufup update identity — a slug, NOT the display title "Better Agent".
# Spaces would leak into archive filenames and target URLs. Must match
# the `app_name` the release repository is initialized with.
APP_NAME = "BetterAgent"

# `paths.ba_home` / `_version` live next to / one level up from this file;
# the frozen bundle puts both on the import path (see BetterAgent.spec),
# and dev/tests add the desktop dir. Make the backend dir importable for
# `paths` in the dev case.
_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _current_version() -> str:
    from _version import __version__
    return __version__


def _ba_home() -> Path:
    from paths import ba_home
    return ba_home()


def update_base_url() -> Optional[str]:
    """The configured update root, defaulting to the bundled primary host."""
    from env_compat import get_env

    url = os.environ.get("BA_UPDATE_URL", "").strip().rstrip("/")
    if url:
        return url
    bundled = _bundled_update_url()
    if bundled:
        return bundled
    port = get_env("BETTER_CLAUDE_BACKEND_PORT", "8000").strip() or "8000"
    return f"http://127.0.0.1:{port}/api/desktop/updates"


def is_enabled() -> bool:
    return True


def _metadata_dir() -> Path:
    return _ba_home() / "update" / "metadata"


def _target_dir() -> Path:
    return _ba_home() / "update" / "targets"


def _app_install_dir() -> Path:
    """The directory tufup replaces on update — the onedir bundle root.

    Frozen: the dir containing the executable. Dev: this source dir (no
    real install to replace; apply is never exercised in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundled_root() -> Optional[Path]:
    """The trusted `root.json` shipped with the app (produced by the
    update-repo init in the release step). None until a build ships it."""
    if getattr(sys, "frozen", False):
        cand = Path(getattr(sys, "_MEIPASS", "")) / "tufup_root.json"
    else:
        cand = Path(__file__).resolve().parent / "tufup_root.json"
    return cand if cand.exists() else None


def _bundled_update_url() -> Optional[str]:
    if getattr(sys, "frozen", False):
        cand = Path(getattr(sys, "_MEIPASS", "")) / "update_url.txt"
    else:
        cand = Path(__file__).resolve().parent / "update_url.txt"
    if not cand.exists():
        return None
    try:
        url = cand.read_text(encoding="utf-8").strip().rstrip("/")
    except OSError:
        return None
    return url or None


def _ensure_trusted_root() -> bool:
    """Seed the trusted root metadata on first run. tufup refuses to run
    without a `root.json` it already trusts; we copy the bundled one in
    once. Returns False when no trusted root is available (→ disabled)."""
    root = _metadata_dir() / "root.json"
    if root.exists():
        return True
    src = _bundled_root()
    if src is None:
        return False
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(src.read_bytes())
    return True


def _build_client():
    from tufup.client import Client

    base = update_base_url()
    return Client(
        app_name=APP_NAME,
        app_install_dir=_app_install_dir(),
        current_version=_current_version(),
        metadata_dir=_metadata_dir(),
        metadata_base_url=f"{base}/metadata/",
        target_dir=_target_dir(),
        target_base_url=f"{base}/targets/",
        refresh_required=False,
    )


def check() -> Optional[str]:
    """Return the available newer version string, or None.

    Non-fatal: disabled config, missing trusted root, and any runtime
    error all yield None so the app still launches (per the chosen
    failure policy)."""
    if not is_enabled():
        return None
    if not _ensure_trusted_root():
        logger.warning("auto-update: no trusted root metadata; skipping")
        return None
    try:
        new = _build_client().check_for_updates()
    except Exception:
        logger.exception("auto-update: check failed (non-fatal)")
        return None
    return str(new.version) if new is not None else None


def apply_and_relaunch() -> None:
    """Download + apply the available update and relaunch.

    Not wrapped in a fallback: a failed apply propagates to the caller.
    tufup's default install moves the new bundle into `app_install_dir`
    and exits; the relaunch of the freshly-installed binary is handled
    by the install step's restart hook."""
    client = _build_client()
    if client.check_for_updates() is None:
        return
    client.download_and_apply_update(skip_confirmation=True)


def start_background_check(
    on_update_available: Callable[[str], None],
) -> Optional[threading.Thread]:
    """Daemon thread: check once for an update and, if one is found, call
    `on_update_available(version)` so the GUI can prompt "restart to
    update". No-op (returns None) when disabled.

    Shell-role only — the backend/runner roles must never call this."""
    if not is_enabled():
        return None

    def _run() -> None:
        version = check()
        if version:
            on_update_available(version)

    t = threading.Thread(target=_run, daemon=True, name="bc-updater")
    t.start()
    return t
