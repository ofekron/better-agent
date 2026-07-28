from __future__ import annotations

import os
import time

import pytest

from restart_request import consume_restart_request, main


def test_valid_restart_request_is_consumed(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    path.write_text("a" * 32, encoding="utf-8")

    assert consume_restart_request(path) == "a" * 32
    assert path.exists() is False


def test_stale_restart_request_is_consumed_without_action(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    path.write_text("b" * 32, encoding="utf-8")

    assert consume_restart_request(path, not_before=time.time() + 1) is None
    assert path.exists() is False


def test_restart_request_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("c" * 32, encoding="utf-8")
    path = tmp_path / "restart_requested"
    path.symlink_to(target)

    with pytest.raises(OSError):
        consume_restart_request(path)

    assert target.read_text(encoding="utf-8") == "c" * 32


def test_restart_request_rejects_oversized_content(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    path.write_bytes(os.urandom(65))

    with pytest.raises(OSError):
        consume_restart_request(path)


def test_clear_removes_stale_request_before_generation_start(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    path.write_text("d" * 32, encoding="utf-8")

    assert main([str(path), "--clear"]) == 0
    assert path.exists() is False
