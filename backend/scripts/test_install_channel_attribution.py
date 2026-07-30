#!/usr/bin/env python3
"""Locks BETTER_AGENT_FROM acquisition-channel attribution.

Covers the allowlist, fail-closed normalization of hostile input, and the
first-attributed-channel-wins record semantics. Runs against an isolated
state home; never touches real Better Agent data.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

import paths  # noqa: E402

HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-install-channel-"))

import install_channel  # noqa: E402

HOSTILE_INPUTS = [
    "; rm -rf /",
    "$(whoami)",
    "`id`",
    "../../etc/passwd",
    "/absolute/path",
    "readme; rm -rf /",
    "readme\nlanding",
    "read\x00me",
    "x" * 10_240,
    "réadme",
    "ＲＥＡＤＭＥ",
    "--mode",
    "unknown;drop",
    "",
    "   ",
    "{}",
    "hn|ph",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_valid_channels_normalize_to_themselves() -> None:
    for channel in install_channel.ALLOWED_CHANNELS:
        got = install_channel.normalize_channel(channel)
        check(got == channel, f"{channel!r} normalized to {got!r}")
        # Case/whitespace tolerance still resolves to the canonical value.
        check(
            install_channel.normalize_channel(f"  {channel.upper()}  ") == channel,
            f"case/space variant of {channel!r} did not normalize",
        )


def test_unset_is_unknown() -> None:
    check(install_channel.channel_from_env({}) == "unknown", "unset env not unknown")
    check(
        install_channel.normalize_channel(None) == "unknown",
        "None not unknown",
    )


def test_hostile_input_is_unknown() -> None:
    for raw in HOSTILE_INPUTS:
        got = install_channel.normalize_channel(raw)
        check(got == "unknown", f"hostile input {raw[:40]!r} resolved to {got!r}")
        got_env = install_channel.channel_from_env({install_channel.CHANNEL_ENV: raw})
        check(got_env == "unknown", f"hostile env {raw[:40]!r} resolved to {got_env!r}")


def _reset_record() -> None:
    record = install_channel.record_path()
    if record.exists():
        record.unlink()


def _record_text() -> str:
    return install_channel.record_path().read_text(encoding="utf-8")


def test_hostile_input_never_written_verbatim() -> None:
    for raw in HOSTILE_INPUTS:
        _reset_record()
        stored = install_channel.record_install_channel(
            "default", {install_channel.CHANNEL_ENV: raw}
        )
        check(stored == "unknown", f"hostile input stored as {stored!r}")
        text = _record_text()
        loaded = json.loads(text)
        check(loaded["channel"] == "unknown", f"record channel is {loaded['channel']!r}")
        check(set(loaded) == {"channel", "installed_at", "mode"}, f"record keys {set(loaded)}")
        needle = raw.strip()
        if needle and needle not in install_channel.ALLOWED_CHANNELS:
            check(needle not in text, f"raw value leaked into record: {needle[:40]!r}")
        # The record path itself must stay inside the isolated home.
        check(
            str(install_channel.record_path()).startswith(HOME),
            "record escaped the test home",
        )


def test_record_writes_valid_channel() -> None:
    _reset_record()
    stored = install_channel.record_install_channel(
        "default", {install_channel.CHANNEL_ENV: "hn"}
    )
    check(stored == "hn", f"expected hn, got {stored!r}")
    loaded = json.loads(_record_text())
    check(loaded["channel"] == "hn", "channel not persisted")
    check(loaded["mode"] == "default", "mode not persisted")
    check(isinstance(loaded["installed_at"], (int, float)), "timestamp missing")


def test_rerun_is_idempotent_first_channel_wins() -> None:
    _reset_record()
    install_channel.record_install_channel("default", {install_channel.CHANNEL_ENV: "ph"})
    first = _record_text()

    # Re-run with no channel must not clobber the attributed channel.
    check(
        install_channel.record_install_channel("desktop-ui-only", {}) == "ph",
        "re-run without channel lost the attribution",
    )
    check(_record_text() == first, "re-run mutated the record")

    # Re-run with a different valid channel must not overwrite either.
    check(
        install_channel.record_install_channel(
            "default", {install_channel.CHANNEL_ENV: "brew"}
        )
        == "ph",
        "second channel overwrote the first",
    )
    check(_record_text() == first, "second channel mutated the record")

    # Exactly one record file, no duplicates/leftovers.
    siblings = sorted(
        p.name
        for p in install_channel.record_path().parent.iterdir()
        if p.name.startswith(install_channel.RECORD_FILENAME)
    )
    check(siblings == [install_channel.RECORD_FILENAME], f"unexpected files {siblings}")


def test_unknown_record_is_upgradable() -> None:
    _reset_record()
    install_channel.record_install_channel("default", {})
    check(
        install_channel.record_install_channel(
            "default", {install_channel.CHANNEL_ENV: "devto"}
        )
        == "devto",
        "unknown record was not upgraded by a later attributed run",
    )
    check(json.loads(_record_text())["channel"] == "devto", "upgrade not persisted")


def test_corrupt_record_is_replaced() -> None:
    install_channel.record_path().write_text("not json{{", encoding="utf-8")
    check(
        install_channel.record_install_channel(
            "default", {install_channel.CHANNEL_ENV: "yt"}
        )
        == "yt",
        "corrupt record was not replaced",
    )
    check(json.loads(_record_text())["channel"] == "yt", "replacement not persisted")


def main() -> int:
    try:
        test_valid_channels_normalize_to_themselves()
        test_unset_is_unknown()
        test_hostile_input_is_unknown()
        test_hostile_input_never_written_verbatim()
        test_record_writes_valid_channel()
        test_rerun_is_idempotent_first_channel_wins()
        test_unknown_record_is_upgradable()
        test_corrupt_record_is_replaced()
    except AssertionError as exc:
        print(f"test_install_channel_attribution: FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print("test_install_channel_attribution: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
