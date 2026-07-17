from pathlib import Path

from paths import ba_home


CACHE_FORMAT_VERSION = 2


def projection_cache_root() -> Path:
    return ba_home() / "app-state" / f"chat-projection-cache-v{CACHE_FORMAT_VERSION}"
