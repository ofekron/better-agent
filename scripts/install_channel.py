#!/usr/bin/env python3
"""Acquisition-channel attribution for the one-command installers.

Single source of truth for the `BETTER_AGENT_FROM` allowlist and its
validation. The shell/PowerShell entry points only pass the raw env var
through; every check lives here so the two surfaces cannot drift.

The raw value arrives from a `curl | bash` surface, so it is treated as
fully untrusted: length-capped, matched against an exact allowlist, and
never used verbatim in a path, command, log line, or on-disk record.
Anything not on the allowlist becomes `unknown` (fail closed).

Only the resolved channel string is recorded — never host, user, IP, or
any other environment data.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import paths

CHANNEL_ENV = "BETTER_AGENT_FROM"
UNKNOWN_CHANNEL = "unknown"

# Exact allowlist. Extend deliberately — anything not listed resolves to
# UNKNOWN_CHANNEL.
ALLOWED_CHANNELS = (
    "readme",
    "landing",
    "hn",
    "ph",
    "devto",
    "yt",
    "gh-trending",
    "dmg",
    "brew",
    UNKNOWN_CHANNEL,
)

# Longest allowed channel is well under this; cap before any comparison so a
# multi-megabyte hostile value is discarded rather than processed.
MAX_RAW_LENGTH = 32

RECORD_FILENAME = "install_attribution.json"


def normalize_channel(raw: str | None) -> str:
    """Resolve an untrusted raw channel value to an allowlisted channel."""
    if not isinstance(raw, str):
        return UNKNOWN_CHANNEL
    if len(raw) > MAX_RAW_LENGTH:
        return UNKNOWN_CHANNEL
    candidate = raw.strip().lower()
    if candidate in ALLOWED_CHANNELS:
        return candidate
    return UNKNOWN_CHANNEL


def channel_from_env(env: dict[str, str] | None = None) -> str:
    """Resolve the channel from `BETTER_AGENT_FROM`; unset => unknown."""
    source = os.environ if env is None else env
    return normalize_channel(source.get(CHANNEL_ENV))


def record_path() -> Path:
    return paths.ba_home() / RECORD_FILENAME


def read_record() -> dict | None:
    path = record_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def record_install_channel(mode: str, env: dict[str, str] | None = None) -> str:
    """Persist the resolved acquisition channel; returns the stored channel.

    First-attributed-channel-wins: once a recognized (non-unknown) channel is
    on disk it is never overwritten, because the acquisition channel is the
    channel the user originally arrived through — a later re-install run
    (often with no channel set at all) is not a new acquisition. A missing,
    corrupt, or still-unknown record is (re)written so a later attributed run
    can fill it in.
    """
    existing = read_record()
    if existing is not None:
        stored = existing.get("channel")
        if isinstance(stored, str) and stored in ALLOWED_CHANNELS and stored != UNKNOWN_CHANNEL:
            return stored

    channel = channel_from_env(env)
    record = {
        "channel": channel,
        "installed_at": time.time(),
        "mode": mode,
    }
    path = record_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    paths.make_private_file(tmp)
    os.replace(tmp, path)
    return channel
