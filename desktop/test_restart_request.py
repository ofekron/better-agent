from __future__ import annotations

import os
import time

import pytest

import switch_control_daemon.line_switch_runtime.restart_request as restart_request
from backend.restart_request import (
    consume_restart_request,
    main,
    new_restart_request_id,
    remove_restart_request,
    valid_restart_request_id,
    write_restart_request,
)


def test_valid_restart_request_is_consumed(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    path.write_text("a" * 32, encoding="utf-8")

    assert consume_restart_request(path) == "a" * 32
    assert path.exists() is False


def test_frontend_uuid_restart_request_is_consumed(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    request_id = "00000000-0000-4000-8000-000000000001"
    path.write_text(request_id, encoding="ascii")

    assert consume_restart_request(path) == request_id


def test_writer_round_trips_generated_request_atomically(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    request_id = new_restart_request_id()

    write_restart_request(path, request_id)

    assert valid_restart_request_id(request_id)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert consume_restart_request(path) == request_id


def test_writer_does_not_require_posix_fchmod(tmp_path, monkeypatch) -> None:
    path = tmp_path / "restart_requested"
    monkeypatch.delattr(restart_request.os, "fchmod")

    write_restart_request(path, "portable-request")

    assert consume_restart_request(path) == "portable-request"


def test_writer_replaces_symlink_without_touching_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    path = tmp_path / "restart_requested"
    path.symlink_to(target)

    write_restart_request(path, "safe-request")

    assert path.is_symlink() is False
    assert consume_restart_request(path) == "safe-request"
    assert target.read_text(encoding="utf-8") == "untouched"


def test_writer_rejects_symlinked_lock_without_touching_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    lock = tmp_path / ".restart_requested.lock"
    lock.symlink_to(target)

    with pytest.raises(OSError):
        write_restart_request(tmp_path / "restart_requested", "safe-request")

    assert target.read_text(encoding="utf-8") == "untouched"


def test_consumer_preserves_concurrent_replacement(tmp_path, monkeypatch) -> None:
    path = tmp_path / "restart_requested"
    path.write_text("old-request", encoding="ascii")
    replacement = tmp_path / "replacement"
    replacement.write_text("new-request", encoding="ascii")
    rename = restart_request.os.rename

    def replace_after_claim(source, claimed) -> None:
        rename(source, claimed)
        restart_request.os.replace(replacement, path)

    monkeypatch.setattr(restart_request.os, "rename", replace_after_claim)

    assert consume_restart_request(path) == "old-request"
    assert path.read_text(encoding="ascii") == "new-request"


def test_conditional_remove_preserves_another_request(tmp_path) -> None:
    path = tmp_path / "restart_requested"
    write_restart_request(path, "new-request")

    assert remove_restart_request(path, "old-request") is False
    assert consume_restart_request(path) == "new-request"

    write_restart_request(path, "owned-request")
    assert remove_restart_request(path, "owned-request") is True
    assert path.exists() is False


@pytest.mark.parametrize(
    "request_id",
    ["", "contains/slash", "contains\nnewline", "é", "a" * 101],
)
def test_writer_rejects_invalid_request_ids(tmp_path, request_id) -> None:
    path = tmp_path / "restart_requested"

    with pytest.raises(ValueError):
        write_restart_request(path, request_id)

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
