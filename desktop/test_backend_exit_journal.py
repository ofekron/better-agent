from __future__ import annotations

import json
import os

import pytest

import backend_exit_journal
from backend_exit_journal import append_backend_exit


def _append(root) -> None:
    append_backend_exit(
        root,
        source="test",
        exit_code=7,
        classification="unexpected",
        decision="restart",
    )


def test_journal_is_private_regular_jsonl(tmp_path) -> None:
    _append(tmp_path)
    path = tmp_path / "backend-exits.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["exit_code"] == 7


def test_journal_rejects_symlink_without_touching_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("preserved", encoding="utf-8")
    (tmp_path / "backend-exits.jsonl").symlink_to(target)

    with pytest.raises(OSError):
        _append(tmp_path)

    assert target.read_text(encoding="utf-8") == "preserved"


def test_journal_rotates_at_bound(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backend_exit_journal, "_MAX_BYTES", 1)
    _append(tmp_path)
    _append(tmp_path)

    current = tmp_path / "backend-exits.jsonl"
    backup = tmp_path / "backend-exits.jsonl.1"
    assert current.is_file()
    assert backup.is_file()
    assert os.path.getsize(backup) > 0
