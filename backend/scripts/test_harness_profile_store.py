from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


TMP_HOME = tempfile.mkdtemp(prefix="better-agent-harness-profile-")
os.environ["BETTER_AGENT_HOME"] = TMP_HOME

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import harness_profile_store


def _payload(name: str, setting_value: str) -> dict:
    return {
        "id": "personal.harness",
        "name": name,
        "base_mode": "bare",
        "extension_instances": [
            {
                "extension_id": "personal.harness",
                "extension_revision": "abc123",
                "surfaces": ["instructions", "skills", "mcp"],
                "mcp_servers": ["personal"],
                "skills": ["personal-harness"],
                "instruction_names": ["personal instructions"],
            }
        ],
        "extension_setting_overlays": {
            "personal.harness": {
                "mode": {"value": setting_value, "schema_hash": "schema-v1"},
            }
        },
        "secret_refs": {
            "personal.harness": ["extension-setting:personal.harness:token"],
        },
        "instruction_sources": [
            {
                "kind": "inline",
                "name": "personal instructions",
                "content": "Follow the packaged harness.",
            }
        ],
        "source": "package-current",
    }


def test_profile_round_trip_and_revision() -> None:
    first = harness_profile_store.upsert_profile(_payload("Personal Harness", "strict"))
    loaded = harness_profile_store.get_profile("personal.harness", first["revision"])
    assert loaded == first
    assert first["base_mode"] == "bare"
    assert first["secret_refs"] == {
        "personal.harness": ["extension-setting:personal.harness:token"]
    }
    second = harness_profile_store.upsert_profile(_payload("Personal Harness", "loose"))
    assert second["created_at"] == first["created_at"]
    assert second["revision"] != first["revision"]
    assert harness_profile_store.get_profile("personal.harness", first["revision"]) is None


def test_rejects_invalid_package_shape() -> None:
    try:
        harness_profile_store.upsert_profile({
            "id": "bad.profile",
            "name": "Bad",
            "base_mode": "bare",
            "extension_instances": [
                {
                    "extension_id": "bad.profile",
                    "extension_revision": "abc123",
                    "surfaces": ["unknown"],
                }
            ],
        })
    except harness_profile_store.HarnessProfileError as exc:
        assert "unsupported surfaces" in str(exc)
    else:
        raise AssertionError("invalid harness profile was accepted")


def main() -> int:
    try:
        test_profile_round_trip_and_revision()
        test_rejects_invalid_package_shape()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS harness profile store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
