from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402

# catalog_cache_path touches ba_home()/model_catalog; isolate so it never
# reaches the real home. (Companion to test_model_catalog_cache_paths.py,
# which covers confinement/stability; this file covers provider-id validation
# and the namespace lstat branches that file leaves open.)
_TEST_HOME = _test_home.TestHome.acquire("ba-test-model-catalog-validation-")

import pytest  # noqa: E402

import model_catalog_paths  # noqa: E402
from model_catalog_paths import (  # noqa: E402
    MAX_PROVIDER_ID_LENGTH,
    CatalogCachePathError,
    cache_filename,
    catalog_cache_path,
)
from paths import ba_home  # noqa: E402


def _namespace() -> Path:
    return ba_home() / "model_catalog"


def _clear_namespace() -> None:
    ns = _namespace()
    if ns.is_symlink():
        ns.unlink()
        return
    if ns.exists():
        # tests here only ever create an empty dir or a plain file at ns
        ns.rmdir() if ns.is_dir() else ns.unlink()


# --- _provider_id_bytes validation (exercised via cache_filename) ------------
# This is the path-injection guard. cache_filename must reject anything that is
# not a clean, single-segment, bounded-length string before it ever hashes it.

@pytest.mark.parametrize("bad", [None, 123, b"bytes", [], object()])
def test_rejects_non_str_provider_id(bad: object) -> None:
    with pytest.raises(CatalogCachePathError, match="invalid provider id"):
        cache_filename(bad)


def test_rejects_empty_provider_id() -> None:
    with pytest.raises(CatalogCachePathError, match="invalid provider id"):
        cache_filename("")


@pytest.mark.parametrize("bad", [" codex", "codex ", "\tcodex", " codex "])
def test_rejects_provider_id_with_outer_whitespace(bad: str) -> None:
    with pytest.raises(CatalogCachePathError, match="invalid provider id"):
        cache_filename(bad)


def test_rejects_overlong_provider_id() -> None:
    with pytest.raises(CatalogCachePathError, match="invalid provider id"):
        cache_filename("a" * (MAX_PROVIDER_ID_LENGTH + 1))


def test_accepts_max_length_provider_id() -> None:
    # boundary: exactly MAX_PROVIDER_ID_LENGTH is valid
    name = cache_filename("a" * MAX_PROVIDER_ID_LENGTH)
    assert name == hashlib.sha256(b"a" * MAX_PROVIDER_ID_LENGTH).hexdigest() + ".json"


@pytest.mark.parametrize("bad", ["cod\x00ex", "cod\x01ex", "cod\x1fex", "cod\x7fex"])
def test_rejects_control_and_del_chars(bad: str) -> None:
    # < 32 control chars and DEL (127) must be rejected
    with pytest.raises(CatalogCachePathError, match="invalid provider id"):
        cache_filename(bad)


def test_cache_filename_is_sha256_json() -> None:
    expected = hashlib.sha256("codex".encode("utf-8")).hexdigest() + ".json"
    assert cache_filename("codex") == expected


# --- catalog_cache_path namespace branches -----------------------------------

def test_cache_path_when_namespace_missing() -> None:
    # FileNotFoundError branch -> path returned, namespace not created
    _clear_namespace()
    assert not _namespace().exists()
    path = catalog_cache_path("codex")
    assert path == _namespace() / cache_filename("codex")
    assert not _namespace().exists()  # lstat-only, never mkdirs


def test_cache_path_when_namespace_is_real_directory() -> None:
    # S_ISDIR true branch -> path returned
    _clear_namespace()
    _namespace().mkdir(parents=True)
    try:
        path = catalog_cache_path("codex")
        assert path.parent == _namespace()
        assert path.name == cache_filename("codex")
    finally:
        _clear_namespace()


def test_cache_path_rejects_non_directory_namespace() -> None:
    # namespace exists but is a file -> S_ISDIR false -> raise
    _clear_namespace()
    _namespace().write_text("not a directory", encoding="utf-8")
    try:
        with pytest.raises(CatalogCachePathError, match="not a directory"):
            catalog_cache_path("codex")
    finally:
        _clear_namespace()


def test_cache_path_raises_when_namespace_lstat_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    # OSError (other than FileNotFoundError) -> raise. Root is a file, so
    # <file>/model_catalog.lstat() raises NotADirectoryError, an OSError subclass.
    file_root = ba_home() / "blocked_root"
    file_root.write_text("x", encoding="utf-8")
    monkeypatch.setattr(model_catalog_paths, "ba_home", lambda: file_root)
    with pytest.raises(CatalogCachePathError, match="namespace unavailable"):
        catalog_cache_path("codex")
