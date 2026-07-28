from __future__ import annotations

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
    ("returncode", "stdout", "expected"),
    [(0, "private\n", True), (2, "", False), (0, "unexpected\n", False)],
)
def test_windows_token_acl_validation_fails_closed(
    monkeypatch, tmp_path, returncode, stdout, expected,
):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(paths.os, "name", "nt")
    monkeypatch.setattr(paths.subprocess, "run", run)
    token_file = tmp_path / "internal token; harmless"

    assert paths.windows_path_has_private_acl(token_file) is expected
    assert captured["command"][-1] == str(token_file)
    assert str(token_file) not in captured["command"][-2]
    assert captured["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
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
