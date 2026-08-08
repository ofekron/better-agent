"""Reproduces `RuntimeError: Claude CLI execution authority mismatch` raised
from `runner.py`'s pinned-launch integrity check (~runner.py:3308).

Root cause: the check compares
`executable = Path(cli.executable_path).resolve(strict=True)` (fully
resolved, symlinks stripped) against
`cli_root = provider_pinned_launch.sdk_launch_cache_root()`, which derives
from the UNRESOLVED `ba_home()`. When `ba_home()` returns a path that
traverses a symlink -- e.g. the `~/.better-agent` alias
`paths._ensure_default_alias` creates pointing at the real
`~/.better-claude`, or any symlinked `BETTER_AGENT_HOME`/test home (macOS
`/tmp` -> `/private/tmp`) -- `executable.is_relative_to(cli_root)` is
`False` for a genuinely-pinned CLI purely because one side is
symlink-resolved and the other isn't, even though both name the identical
directory. This is the same class of bug as the `session_store._sessions_dir()`
fix in `test_session_storage_identity_race.py`.

Same construction as that sibling test: an explicit real dir + symlink
alias in `tmp_path`, so the test doesn't depend on the host's actual home
layout.
"""
from __future__ import annotations

from pathlib import Path

import provider_pinned_launch


def _fake_cache_layout(real_home: Path) -> Path:
    """Build a plausible `sdk_launch_cache_root()`-shaped tree under the
    REAL (non-symlinked) home and return the materialized "executable"
    path inside it -- standing in for a CLI that
    `materialize_sdk_launch_cached` already pinned into the shared cache on
    a prior turn."""
    fingerprint = "a" * 64
    root_dir = real_home / "cache" / "sdk-launches" / fingerprint / "root"
    root_dir.mkdir(parents=True)
    executable = root_dir / "claude"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o700)
    return executable


def test_genuinely_pinned_cli_matches_through_symlinked_home(monkeypatch, tmp_path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias_home = tmp_path / "alias-home"
    alias_home.symlink_to(real_home)

    # Stand in for `ba_home()` resolving to the symlinked alias, as happens
    # in production once `paths._ensure_default_alias` has created
    # `~/.better-agent` -> `~/.better-claude` on a prior boot.
    monkeypatch.setattr(provider_pinned_launch, "ba_home", lambda: alias_home)

    executable_raw = _fake_cache_layout(real_home)

    # Mirrors runner.py's pinned-launch check verbatim (runner.py:3291-3294):
    #   cli_root = sdk_launch_cache_root()
    #   executable = Path(cli.executable_path).resolve(strict=True)
    #   ... not executable.is_relative_to(cli_root) ...
    cli_root = provider_pinned_launch.sdk_launch_cache_root()
    executable = executable_raw.resolve(strict=True)

    assert executable.is_relative_to(cli_root), (
        f"false-positive authority mismatch: {executable} vs {cli_root} "
        "-- same real directory reached through a symlinked home"
    )


def test_genuinely_different_home_still_fails_closed(monkeypatch, tmp_path):
    """Same-home symlink aliasing must not weaken the guard's actual job:
    an executable that lives outside the pinned cache entirely is still a
    genuine authority mismatch and must still fail the check."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias_home = tmp_path / "alias-home"
    alias_home.symlink_to(real_home)
    monkeypatch.setattr(provider_pinned_launch, "ba_home", lambda: alias_home)

    # Force the cache root to be materialized/created.
    cli_root = provider_pinned_launch.sdk_launch_cache_root()

    other_home = tmp_path / "unrelated-home"
    rogue_dir = other_home / "somewhere" / "else"
    rogue_dir.mkdir(parents=True)
    rogue_executable = rogue_dir / "claude"
    rogue_executable.write_bytes(b"#!/bin/sh\n")
    rogue_executable.chmod(0o700)

    executable = rogue_executable.resolve(strict=True)
    assert not executable.is_relative_to(cli_root)
