"""Locks the rollout-path commit-race fix for the Codex provider.

Reproduces the failure behind "codex state missing native rollout path": Codex's
app-server emits thread.started before the thread row is committed to the codex
sqlite DB, so a single resolve at that instant returns None. The polled resolver
must recover; the single-shot resolver must not.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _test_home
_test_home.isolate("bc_test_")

import codex_native


def _seed_state_db(db_path: Path, *, thread_id: str, rollout: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("create table threads (id text primary key, rollout_path text not null)")
        conn.execute(
            "insert into threads (id, rollout_path) values (?, ?)",
            (thread_id, str(rollout)),
        )
        conn.commit()
    finally:
        conn.close()


def _late_commit_resolver(real_path: Path, commit_after: int):
    """Mimics codex committing the thread row only after N queries."""
    state = {"n": 0}

    def fake(thread_id: str):
        state["n"] += 1
        if state["n"] < commit_after:
            return None  # row not committed yet
        return real_path

    fake.calls = state  # type: ignore[attr-defined]
    return fake


def test_polled_resolver_recovers_from_late_commit(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    fake = _late_commit_resolver(rollout, commit_after=3)
    original = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = fake  # type: ignore[assignment]
    try:
        result = asyncio.run(
            codex_native.resolve_rollout_path_polled(
                "thread-1", timeout=5.0, poll_interval=0.001
            )
        )
    finally:
        codex_native.resolve_rollout_path = original  # type: ignore[assignment]

    assert result == rollout
    assert fake.calls["n"] == 3  # retried until the row appeared


def test_single_shot_resolver_misses_late_commit(tmp_path):
    """The pre-fix behaviour: a single resolve returns None while the codex
    sqlite row is still uncommitted — exactly the condition that surfaced the
    'missing native rollout path' failure."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    fake = _late_commit_resolver(rollout, commit_after=3)
    original = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = fake  # type: ignore[assignment]
    try:
        result = codex_native.resolve_rollout_path("thread-1")
    finally:
        codex_native.resolve_rollout_path = original  # type: ignore[assignment]

    assert result is None  # first call lands before the commit — the bug
    assert fake.calls["n"] == 1


def test_polled_resolver_gives_up_after_timeout():
    def always_none(thread_id: str):
        return None

    original = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = always_none  # type: ignore[assignment]
    try:
        result = asyncio.run(
            codex_native.resolve_rollout_path_polled(
                "thread-1", timeout=0.05, poll_interval=0.01
            )
        )
    finally:
        codex_native.resolve_rollout_path = original  # type: ignore[assignment]

    assert result is None  # genuine miss still fails closed, no infinite loop


def test_resolver_checks_live_codex_state_db_before_legacy_sqlite_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    thread_id = "thread-live-db"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")

    _seed_state_db(home / ".codex" / "state_5.sqlite", thread_id=thread_id, rollout=rollout)
    _seed_state_db(
        home / ".codex" / "sqlite" / "state_5.sqlite",
        thread_id="other-thread",
        rollout=rollout,
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    assert codex_native.resolve_rollout_path(thread_id) == rollout


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
