from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_CLI_DIRS = (
    "/usr/local/lib/npm-global/bin",
    "~/.npm-global/bin",
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def resolve_cli_binary(name: str, extra_dirs: Iterable[str] = ()) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found

    for raw_dir in [*extra_dirs, *DEFAULT_CLI_DIRS]:
        candidate = Path(os.path.expanduser(raw_dir)) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
