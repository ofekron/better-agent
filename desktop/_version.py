"""Single source of truth for the desktop app version.

Read by `BetterAgent.spec` (CFBundleShortVersionString / CFBundleVersion)
and by `desktop/updater.py` (tufup `current_version`). Bump on every
release that is pushed to the update repository.
"""

__version__ = "0.1.1784454123"
