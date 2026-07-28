from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from paths import ba_home


MAX_PROVIDER_ID_LENGTH = 128
_CACHE_DIRECTORY = "model_catalog"


class CatalogCachePathError(RuntimeError):
    pass


def _provider_id_bytes(provider_id: str) -> bytes:
    if (
        type(provider_id) is not str
        or not provider_id
        or provider_id.strip() != provider_id
        or len(provider_id) > MAX_PROVIDER_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in provider_id)
    ):
        raise CatalogCachePathError("invalid provider id")
    return provider_id.encode("utf-8")


def cache_filename(provider_id: str) -> str:
    digest = hashlib.sha256(_provider_id_bytes(provider_id)).hexdigest()
    return f"{digest}.json"


def catalog_cache_path(provider_id: str) -> Path:
    root = ba_home()
    namespace = root / _CACHE_DIRECTORY
    try:
        observed = namespace.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CatalogCachePathError("catalog cache namespace unavailable") from exc
    else:
        if not stat.S_ISDIR(observed.st_mode):
            raise CatalogCachePathError("catalog cache namespace is not a directory")
    return namespace / cache_filename(provider_id)
