"""Unit coverage for the remaining branches of model_catalog_cache.py.

The happy-path round-trip, digest-tamper, oversize, symlink, concurrency, and
strict-shape paths are already covered by test_model_catalog_cache_schema3.py.
This file closes the residual branches: from_dict validation mutations,
build/read/write input-type guards, the digest property, the missing-file read,
and the TOCTOU OSError guards in the low-level file-read helpers.

The TOCTOU guards (_same_path_identity / _load_snapshot OSError arms, the
read loop's size ceiling, unlink-on-cleanup) are exercised by patching the
``os`` callables the module references, the same pattern
test_model_catalog_cache_schema3.py uses for os.open/os.read/os.fstat.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import _test_home

_test_home.isolate("bc-test-model-catalog-cache-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import model_catalog_cache as catalog_cache  # noqa: E402
from model_catalog_cache import (  # noqa: E402
    MAX_MODELS,
    MAX_RETIRED,
    CatalogCacheError,
    CatalogSnapshot,
    build_catalog_snapshot,
    read_catalog_cache,
    write_catalog_cache,
)
from test_model_catalog_cache_schema3 import _authority, _snapshot  # noqa: E402


@contextmanager
def _authority_ctx():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        yield root, _authority(root)


def _base_snapshot_dict(authority) -> dict:
    return _snapshot(authority).to_dict()


# ===========================================================================
# CatalogSnapshot.to_dict / digest property
# ===========================================================================

class TestSnapshotDigest:
    def test_digest_property_matches_embedded_digest(self):
        with _authority_ctx() as (_root, authority):
            snapshot = _snapshot(authority)
            full = snapshot.to_dict()
            assert "digest" in full
            body = snapshot.to_dict(include_digest=False)
            assert "digest" not in body
            assert snapshot.digest == full["digest"]


# ===========================================================================
# CatalogSnapshot.from_dict validation branches
# ===========================================================================

class TestFromDictValidation:
    def test_unsupported_schema_rejected(self):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["schema"] = 2
        with pytest.raises(CatalogCacheError, match="unsupported catalog cache schema"):
            CatalogSnapshot.from_dict(bad)

    def test_active_retired_overlap_rejected(self):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["models"] = ["shared", "other"]
        bad["retired"] = [{"id": "shared", "first_absent_at": 1.0}]
        with pytest.raises(CatalogCacheError, match="overlap"):
            CatalogSnapshot.from_dict(bad)

    def test_too_many_models_rejected(self):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["models"] = ["m"] * (MAX_MODELS + 1)
        with pytest.raises(CatalogCacheError, match="invalid model list"):
            CatalogSnapshot.from_dict(bad)

    def test_too_many_retired_rejected(self):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["retired"] = [
            {"id": f"r{i}", "first_absent_at": 1.0}
            for i in range(MAX_RETIRED + 1)
        ]
        with pytest.raises(CatalogCacheError, match="invalid retired model list"):
            CatalogSnapshot.from_dict(bad)

    @pytest.mark.parametrize(
        "retired",
        [
            [{"id": "r"}],            # missing first_absent_at
            ["not-a-dict"],           # non-dict entry
            [{"id": "r", "first_absent_at": 1.0, "extra": 1}],  # unknown key
        ],
    )
    def test_malformed_retired_entry_rejected(self, retired):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["retired"] = retired
        with pytest.raises(CatalogCacheError, match="invalid retired model"):
            CatalogSnapshot.from_dict(bad)

    def test_duplicate_retired_id_rejected(self):
        with _authority_ctx() as (_root, authority):
            bad = _base_snapshot_dict(authority)
        bad["retired"] = [
            {"id": "dup", "first_absent_at": 1.0},
            {"id": "dup", "first_absent_at": 2.0},
        ]
        with pytest.raises(CatalogCacheError, match="duplicate retired model id"):
            CatalogSnapshot.from_dict(bad)


# ===========================================================================
# build_catalog_snapshot input-type guard
# ===========================================================================

class TestBuildInputTypes:
    def test_non_catalog_authority_rejected(self):
        with pytest.raises(CatalogCacheError, match="invalid catalog snapshot input"):
            build_catalog_snapshot(
                authority={"not": "an authority"},
                models=[],
                retired=[],
                last_refreshed_at=1.0,
                last_fetch_state="ok",
            )

    def test_models_wrong_type_rejected(self):
        with _authority_ctx() as (_root, authority):
            with pytest.raises(CatalogCacheError, match="invalid catalog snapshot input"):
                build_catalog_snapshot(
                    authority=authority,
                    models="not-a-sequence",
                    retired=[],
                    last_refreshed_at=1.0,
                    last_fetch_state="ok",
                )

    def test_retired_wrong_type_rejected(self):
        with _authority_ctx() as (_root, authority):
            with pytest.raises(CatalogCacheError, match="invalid catalog snapshot input"):
                build_catalog_snapshot(
                    authority=authority,
                    models=[],
                    retired="not-a-sequence",
                    last_refreshed_at=1.0,
                    last_fetch_state="ok",
                )


# ===========================================================================
# read_catalog_cache / write_catalog_cache public guards
# ===========================================================================

class TestPublicGuards:
    def test_read_requires_catalog_authority_type(self):
        with tempfile.TemporaryDirectory() as raw:
            with pytest.raises(CatalogCacheError, match="expected catalog authority"):
                read_catalog_cache(
                    Path(raw) / "models.json",
                    expected_authority=None,
                )

    def test_read_missing_file_returns_missing(self):
        with _authority_ctx() as (root, authority):
            result = read_catalog_cache(
                root / "absent.json",
                expected_authority=authority,
            )
        assert result.status == "missing"
        assert result.snapshot is None

    def test_write_rejects_non_path(self):
        with _authority_ctx() as (_root, authority):
            snapshot = _snapshot(authority)
            with pytest.raises(CatalogCacheError, match="invalid catalog cache write"):
                write_catalog_cache("not-a-path", snapshot)

    def test_write_rejects_non_snapshot(self):
        with _authority_ctx() as (root, authority):
            with pytest.raises(CatalogCacheError, match="invalid catalog cache write"):
                write_catalog_cache(root / "models.json", {"not": "a snapshot"})


# ===========================================================================
# TOCTOU / OSError guards in the low-level file-read helpers
# ===========================================================================

class TestFileReadToctou:
    def test_file_vanishes_after_lstat_returns_missing(self, monkeypatch):
        # _read_bytes os.open raises FileNotFoundError -> _load_snapshot
        # returns None -> read reports missing.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            write_catalog_cache(path, _snapshot(authority))
            real_open = catalog_cache.os.open

            def vanishing_open(raw_path, flags, *args, **kwargs):
                if Path(raw_path) == path:
                    raise FileNotFoundError("vanished")
                return real_open(raw_path, flags, *args, **kwargs)

            monkeypatch.setattr(catalog_cache.os, "open", vanishing_open)
            result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "missing"

    def test_lstat_oserror_returns_invalid(self, monkeypatch):
        # path.lstat() holds its own accessor, so patch Path.lstat directly.
        # A non-FileNotFoundError OSError surfaces as an invalid cache.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            write_catalog_cache(path, _snapshot(authority))
            real_lstat = Path.lstat

            def raising_lstat(self):
                if self == path:
                    raise OSError("lstat boom")
                return real_lstat(self)

            monkeypatch.setattr(Path, "lstat", raising_lstat)
            result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "invalid"

    def test_stat_identity_changed_during_read_returns_invalid(self, monkeypatch):
        # os.fstat's second call (the post-read `after` identity check)
        # reports a shifted mtime -> stable identity mismatch -> rejected.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            write_catalog_cache(path, _snapshot(authority))
            real_fstat = catalog_cache.os.fstat
            calls = {"n": 0}

            def shifting_fstat(fd):
                observed = real_fstat(fd)
                calls["n"] += 1
                if calls["n"] == 2:
                    return SimpleNamespace(
                        st_mode=observed.st_mode,
                        st_size=observed.st_size,
                        st_dev=observed.st_dev,
                        st_ino=observed.st_ino,
                        st_mtime_ns=observed.st_mtime_ns + 1,
                        st_ctime_ns=observed.st_ctime_ns,
                        st_atime_ns=observed.st_atime_ns,
                    )
                return observed

            monkeypatch.setattr(catalog_cache.os, "fstat", shifting_fstat)
            result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "invalid"

    def test_directory_path_removed_then_invalid(self):
        # A non-regular, non-symlink entry (directory) hits the
        # _remove_if_same(path, None) path with is_symlink() False.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            path.mkdir()
            result = read_catalog_cache(path, expected_authority=authority)
            assert result.status == "invalid"
            assert path.is_dir()  # not unlinked (not a symlink)

    def test_read_exceeding_size_ceiling_during_read(self, monkeypatch):
        # fstat reports a small file (passes the size precheck), but os.read
        # returns more than MAX_CACHE_BYTES total -> the read loop exits via
        # the while condition and the post-loop size check rejects it.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            write_catalog_cache(path, _snapshot(authority))

            def growing_read(descriptor, size):
                # Never return EOF; always fill the requested chunk so the
                # loop only terminates by exhausting `remaining`.
                return b"x" * size

            monkeypatch.setattr(catalog_cache.os, "read", growing_read)
            result = read_catalog_cache(path, expected_authority=authority)
            assert not path.exists()  # poisoned cache removed
        assert result.status == "invalid"

    def test_post_parse_identity_loss_returns_invalid(self, monkeypatch):
        # File parses, but path.lstat() raises OSError during the final
        # _same_path_identity check -> returns False -> rejected.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            write_catalog_cache(path, _snapshot(authority))
            real_lstat = Path.lstat
            calls = {"n": 0}

            def flaky_lstat(self):
                if self == path:
                    calls["n"] += 1
                    if calls["n"] >= 2:
                        raise OSError("flaky")
                return real_lstat(self)

            monkeypatch.setattr(Path, "lstat", flaky_lstat)
            result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "invalid"

    def test_unlink_oserror_during_cleanup_swallowed(self, monkeypatch):
        # _parse_snapshot fails -> _remove_if_same attempts unlink, which
        # raises OSError -> swallowed (guard), cache still reported invalid.
        with _authority_ctx() as (root, authority):
            path = root / "models.json"
            # Valid authority + valid size, but a digest-mismatched body so
            # _parse_snapshot raises CatalogCacheError after a clean read.
            payload = _snapshot(authority).to_dict()
            payload["digest"] = "a" * 64  # wrong digest, keeps SHA256 shape
            path.write_text(json.dumps(payload), encoding="utf-8")

            real_unlink = Path.unlink

            def raising_unlink(self, *args, **kwargs):
                if self == path:
                    raise OSError("unlink boom")
                return real_unlink(self, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", raising_unlink)
            result = read_catalog_cache(path, expected_authority=authority)
            assert path.exists()  # unlink was swallowed, file remains
        assert result.status == "invalid"
