#!/usr/bin/env python3
"""Version-bump lock for the harness-profile store's migration chain.

Fails when a schema bump lands without its N->N+1 migration edge, when the
5->6 provider-pin re-expression loses the pin, or when an unsupported store
version is silently accepted."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-harness-migrations-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
os.environ["BETTER_CLAUDE_HOME"] = str(TEST_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import config_store  # noqa: E402
import harness_profile_store as hps  # noqa: E402


def _write_v5_store(profiles: dict[str, dict], version: int = 5) -> None:
    db = TEST_HOME / "harness_profiles.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "CREATE TABLE profiles (id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
            "revision TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(version),)
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('store_version', '3')")
        for pid, data in profiles.items():
            conn.execute(
                "INSERT INTO profiles(id, schema_version, revision, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, version, str(uuid.uuid4()), "2026-01-01T00:00:00", json.dumps(data)),
            )
    conn.close()


def _v5_profile(pid: str, **extra) -> dict:
    return {
        "id": pid,
        "schema_version": 5,
        "name": pid,
        "description": "",
        "base_profile_id": None,
        "base_profile_revision": None,
        "default_provider_id": None,
        "default_model": None,
        "default_reasoning_effort": None,
        "provisioning_prompt": None,
        "extension_instances": {},
        "overrides": {},
        "mcp_overrides": {},
        "skill_overrides": {},
        "native_harness_overrides": {},
        "provider_run_config_overlay": {},
        "secret_refs": {},
        "capability_contexts": [],
        **extra,
    }


def main() -> int:
    try:
        hps._validate_schema_migrations()
        assert set(hps._SCHEMA_MIGRATIONS) == set(
            range(hps.MIN_SUPPORTED_SCHEMA_VERSION, hps.SCHEMA_VERSION)
        ), "every version in [MIN, CURRENT) needs a migration edge"
        print("PASS migration chain is contiguous")

        provider = config_store.list_provider_ui_state()["providers"][0]
        expected_profile_id = config_store.provider_execution_defaults(
            provider["id"]
        )["runtime_profile_id"]
        assert expected_profile_id

        _write_v5_store({
            "pinned": _v5_profile("pinned", default_provider_id=provider["id"]),
            "ghostpin": _v5_profile("ghostpin", default_provider_id="ghost-provider"),
            "plain": _v5_profile("plain", default_model="kept-model"),
        })
        migrated = hps.get_profile("pinned")
        assert migrated["default_runtime_profile_id"] == expected_profile_id, (
            "provider pin must map to the provider's preferred live profile"
        )
        assert "default_provider_id" not in migrated
        assert hps.get_profile("ghostpin")["default_runtime_profile_id"] is None
        assert hps.get_profile("plain")["default_model"] == "kept-model"
        conn = sqlite3.connect(str(TEST_HOME / "harness_profiles.db"))
        stored = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        conn.close()
        assert int(stored) == hps.SCHEMA_VERSION
        print("PASS 5->6 provider pin re-expression")

        _write_v5_store({"old": _v5_profile("old")}, version=4)
        try:
            hps.get_profile("old")
        except hps.HarnessProfileError as exc:
            assert "incompatible" in str(exc)
        else:
            raise AssertionError("below-minimum store version was accepted")
        print("PASS below-minimum version refused")

        _write_v5_store({"future": _v5_profile("future")}, version=hps.SCHEMA_VERSION + 1)
        try:
            hps.get_profile("future")
        except hps.HarnessProfileError as exc:
            assert "incompatible" in str(exc)
        else:
            raise AssertionError("newer store version was accepted")
        print("PASS newer version refused")
        print("OK")
        return 0
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
