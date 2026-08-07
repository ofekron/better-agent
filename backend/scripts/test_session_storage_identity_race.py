"""Reproduces the `POST /api/sessions` -> 500 seen on real backend boots:
`RuntimeError: session storage identity is active in another scope` raised
from `session_store._sessions_dir()` (session_store.py:168).

Root cause: `_sessions_dir()`'s no-scope default path computes
`ba_home() / "sessions"` WITHOUT `.resolve()`, while `_storage_identity_scope`
(used by every background actor -- session_queue_projection's
`ensure_current_or_rebuild` at backend boot, `producer_lease`, etc.) always
`.resolve()`s the identity it registers as `_active_storage_identity`. When
`ba_home()` returns a path that traverses a symlink -- e.g. the
`~/.better-agent` alias `paths._ensure_default_alias` creates pointing at
the real `~/.better-claude`, or any symlinked `BETTER_AGENT_HOME` -- the two
sides name the IDENTICAL directory with two different strings, and the
equality guard in `_sessions_dir()` spuriously fires even though there is no
real cross-home conflict.

This test forces that exact condition with an explicit real dir + symlink
alias so it doesn't depend on the host's actual home layout, then drives the
two contending actors concurrently: a background thread holding
`_storage_identity_scope` open (standing in for
`session_queue_projection.storage_context()` at boot), and the main thread
calling the plain `_sessions_dir()` path used by ordinary session creation.
"""
from __future__ import annotations

import threading
from pathlib import Path

import session_store


def test_default_sessions_dir_matches_symlinked_background_scope(monkeypatch, tmp_path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias_home = tmp_path / "alias-home"
    alias_home.symlink_to(real_home)

    # Stand in for `ba_home()` resolving to the symlinked alias, as happens
    # in production once `paths._ensure_default_alias` has created
    # `~/.better-agent` -> `~/.better-claude` on a prior boot.
    monkeypatch.setattr(session_store, "ba_home", lambda: alias_home)

    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_background_scope() -> None:
        # Stands in for `session_queue_projection.storage_context()`, which
        # always resolves the identity it passes to `_storage_identity_scope`.
        try:
            identity = (real_home / "sessions").resolve()
            with session_store._storage_identity_scope(identity, wait=True):
                entered.set()
                assert release.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    worker = threading.Thread(
        target=hold_background_scope,
        name="queue-projection-bg",
    )
    worker.start()
    try:
        assert entered.wait(timeout=5.0)
        # Ordinary request-path session creation: no scope entered on this
        # thread, so `_STORAGE_IDENTITY` is unset and `_sessions_dir()` falls
        # back to the plain `ba_home() / "sessions"` branch.
        result = session_store._sessions_dir()
        assert result == (real_home / "sessions").resolve()
    finally:
        release.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
    assert not errors, errors


def test_genuinely_different_home_still_fails_closed(monkeypatch, tmp_path):
    """Same-home symlink aliasing must not weaken the guard's actual job:
    two truly different real directories are still a genuine identity
    conflict and must still raise immediately, with no cache mutation."""
    primary_home = tmp_path / "primary"
    primary_home.mkdir()
    other_home = tmp_path / "other"
    other_home.mkdir()

    monkeypatch.setattr(session_store, "ba_home", lambda: other_home)

    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_background_scope() -> None:
        try:
            identity = (primary_home / "sessions").resolve()
            with session_store._storage_identity_scope(identity, wait=True):
                entered.set()
                assert release.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(
        target=hold_background_scope,
        name="queue-projection-bg",
    )
    worker.start()
    try:
        assert entered.wait(timeout=5.0)
        raised = False
        try:
            session_store._sessions_dir()
        except RuntimeError:
            raised = True
        assert raised, "genuinely different home must still fail closed"
    finally:
        release.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
    assert not errors, errors
