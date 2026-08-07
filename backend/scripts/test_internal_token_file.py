from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import internal_token_file
import json_store
import orchestrator
import paths

SPAWN_TOKEN = "A" * 43
ROTATED_TOKEN = "B" * 43


def write_token(root: Path, token: str) -> Path:
    token_file = root / "internal_token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    return token_file


def test_internal_token_publication_is_atomic(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, SPAWN_TOKEN)
    observed = {}
    original_replace = json_store._replace_atomic
    original_make_private = paths.make_private_file

    def observe_replace(source, destination):
        observed["old"] = destination.read_text(encoding="utf-8")
        observed["new"] = source.read_text(encoding="utf-8")
        observed["mode"] = source.stat().st_mode & 0o777
        original_replace(source, destination)

    def observe_make_private(path):
        observed["secured"] = True
        original_make_private(path)

    monkeypatch.setattr(
        orchestrator,
        "_internal_token_path",
        lambda: token_file,
    )
    monkeypatch.setattr(paths, "make_private_file", observe_make_private)
    monkeypatch.setattr(json_store, "_replace_atomic", observe_replace)
    orchestrator._persist_internal_token(ROTATED_TOKEN)

    assert observed == {
        "old": SPAWN_TOKEN,
        "new": ROTATED_TOKEN,
        "mode": 0o600,
        "secured": True,
    }
    assert token_file.read_text(encoding="utf-8") == ROTATED_TOKEN


@pytest.mark.parametrize(
    ("observed", "expected"),
    [(True, True), (False, False), (OSError("ACL read failed"), False)],
)
def test_windows_token_acl_validation_fails_closed(
    monkeypatch, tmp_path, observed, expected,
):
    captured = {}

    class Security:
        def has_private_acl(self, path, *, user_sid):
            captured["path"] = path
            captured["user_sid"] = user_sid
            if isinstance(observed, BaseException):
                raise observed
            return observed

    security = Security()
    monkeypatch.setattr(paths.os, "name", "nt")
    monkeypatch.setattr(paths, "_windows_security", lambda: security)
    monkeypatch.setattr(
        paths,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-current",
    )
    token_file = tmp_path / "internal token; harmless"

    assert paths.windows_path_has_private_acl(token_file) is expected
    assert captured == {
        "path": token_file,
        "user_sid": "S-1-5-21-current",
    }


def test_windows_token_reader_rejects_non_private_acl(
    monkeypatch, tmp_path,
):
    token_file = tmp_path / "internal_token"
    token_file.write_text(ROTATED_TOKEN, encoding="utf-8")
    identity = token_file.stat()
    monkeypatch.setattr(internal_token_file.os, "name", "nt")
    monkeypatch.setattr(
        paths,
        "windows_path_has_private_acl",
        lambda path: False,
    )

    assert not internal_token_file._is_private_identity(token_file, identity)


def test_windows_token_reader_rejects_reparse_points():
    identity = SimpleNamespace(st_file_attributes=0x400)

    assert internal_token_file._is_windows_reparse_point(identity)


def test_backend_startup_rotates_unsafe_legacy_token(monkeypatch, tmp_path):
    token_file = tmp_path / "internal_token"
    token_file.write_text(ROTATED_TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.setattr(
        orchestrator,
        "_internal_token_path",
        lambda: token_file,
    )

    loaded = orchestrator._load_or_create_internal_token()

    assert loaded != ROTATED_TOKEN
    assert internal_token_file.read_private_token(token_file) == loaded


def test_backend_startup_preserves_private_token(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, ROTATED_TOKEN)
    monkeypatch.setattr(
        orchestrator,
        "_internal_token_path",
        lambda: token_file,
    )

    assert orchestrator._load_or_create_internal_token() == ROTATED_TOKEN


def _private(root: Path, name: str, body: bytes | str) -> Path:
    token_file = root / name
    if isinstance(body, str):
        token_file.write_text(body, encoding="utf-8")
    else:
        token_file.write_bytes(body)
    token_file.chmod(0o600)
    return token_file


def test_reader_rejects_non_regular_lstat(tmp_path):
    # A symlink is rejected by the lstat shape check before it is ever opened.
    target = write_token(tmp_path, SPAWN_TOKEN)
    link = tmp_path / "link"
    link.symlink_to(target)

    assert internal_token_file.read_private_token(link) is None


def test_reader_rejects_wrong_size_tokens(tmp_path):
    empty = _private(tmp_path, "empty", "")
    assert internal_token_file.read_private_token(empty) is None

    oversize = _private(tmp_path, "oversize", "x" * (internal_token_file._MAX_TOKEN_BYTES + 1))
    assert internal_token_file.read_private_token(oversize) is None


def test_reader_rejects_non_ascii_token(tmp_path):
    binary = _private(tmp_path, "binary", b"\xff" * 43)
    assert internal_token_file.read_private_token(binary) is None


def test_reader_rejects_short_token(tmp_path):
    short = _private(tmp_path, "short", "abc")
    assert internal_token_file.read_private_token(short) is None


def test_write_rejects_invalid_token_shape(tmp_path):
    with pytest.raises(ValueError, match="invalid shape"):
        internal_token_file.write_private_token(tmp_path / "token", "bad token!")


def test_reader_rejects_toctou_non_regular_after_open(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, SPAWN_TOKEN)
    monkeypatch.setattr(
        internal_token_file.os,
        "fstat",
        lambda fd: os.stat_result((stat.S_IFCHR | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
    )

    assert internal_token_file.read_private_token(token_file) is None


def test_reader_rejects_toctou_identity_swap(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, SPAWN_TOKEN)
    real = os.stat(token_file)
    swapped = os.stat_result(
        (stat.S_IFREG | 0o600, real.st_ino + 1, real.st_dev + 1,
         real.st_nlink, os.getuid(), os.getgid(), len(SPAWN_TOKEN),
         real.st_atime, real.st_mtime, real.st_ctime),
    )
    monkeypatch.setattr(internal_token_file.os, "fstat", lambda fd: swapped)

    assert internal_token_file.read_private_token(token_file) is None


def test_reader_returns_none_on_read_oserror(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, SPAWN_TOKEN)

    def _boom(fd, n):
        raise OSError("io error")

    monkeypatch.setattr(internal_token_file.os, "read", _boom)
    assert internal_token_file.read_private_token(token_file) is None


def test_reader_rejects_oversize_chunk_growth(monkeypatch, tmp_path):
    token_file = write_token(tmp_path, SPAWN_TOKEN)
    monkeypatch.setattr(
        internal_token_file.os,
        "read",
        lambda fd, n: b"x" * (internal_token_file._MAX_TOKEN_BYTES + 1),
    )

    assert internal_token_file.read_private_token(token_file) is None
