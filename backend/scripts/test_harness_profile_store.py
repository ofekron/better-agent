from __future__ import annotations

import json
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


def test_create_profile_has_empty_overrides() -> None:
    profile = harness_profile_store.create_profile({
        "id": "personal.harness",
        "name": "Personal Harness",
        "description": "hand-tuned overrides",
    })
    assert profile["schema_version"] == 2
    assert profile["overrides"] == {}
    loaded = harness_profile_store.get_profile("personal.harness")
    assert loaded == profile


def test_create_profile_ignores_client_sent_overrides() -> None:
    profile = harness_profile_store.create_profile({
        "id": "sneaky.harness",
        "name": "Sneaky",
        "overrides": {"disabled_builtin_tools": {"add": ["mssg"], "remove": []}},
    })
    assert profile["overrides"] == {}


def test_overrides_delta_add_remove_roundtrip() -> None:
    harness_profile_store.create_profile({"id": "delta.harness", "name": "Delta"})
    updated = harness_profile_store.apply_override_patch(
        "delta.harness",
        [
            {
                "path": ["disabled_builtin_tools"],
                "op": "set",
                "value": {"add": ["mssg", "ask"], "remove": ["create_session"]},
            },
            {
                "path": ["extension_instances", "personal.harness", "mcp_servers"],
                "op": "set",
                "value": {"add": ["personal"], "remove": []},
            },
        ],
    )
    assert updated["overrides"]["disabled_builtin_tools"] == {
        "add": ["mssg", "ask"], "remove": ["create_session"],
    }
    assert updated["overrides"]["extension_instances"]["personal.harness"]["mcp_servers"] == {
        "add": ["personal"], "remove": [],
    }
    cleared = harness_profile_store.apply_override_patch(
        "delta.harness",
        [{"path": ["disabled_builtin_tools"], "op": "clear"}],
    )
    assert "disabled_builtin_tools" not in cleared["overrides"]
    assert "extension_instances" in cleared["overrides"]


def test_default_is_not_a_stored_profile() -> None:
    try:
        harness_profile_store.get_profile("default")
    except harness_profile_store.HarnessProfileError as exc:
        assert "default" in str(exc)
    else:
        raise AssertionError("get_profile('default') should have raised")


def test_delete_default_rejected() -> None:
    try:
        harness_profile_store.delete_profile("default")
    except harness_profile_store.HarnessProfileError as exc:
        assert "default" in str(exc)
    else:
        raise AssertionError("delete_profile('default') should have raised")


def test_create_default_rejected() -> None:
    try:
        harness_profile_store.create_profile({"id": "default", "name": "nope"})
    except harness_profile_store.HarnessProfileError as exc:
        assert "default" in str(exc)
    else:
        raise AssertionError("create_profile('default') should have raised")


def test_schema_version_2_mismatch_raises() -> None:
    path = harness_profile_store._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "profiles": {}}), encoding="utf-8")
    try:
        harness_profile_store.list_profiles()
    except harness_profile_store.HarnessProfileError:
        pass
    else:
        raise AssertionError("v1-shaped file should have raised on load")
    finally:
        # Reset to a fresh v2 blank store for any tests that run after this one.
        path.write_text(json.dumps(harness_profile_store._blank()), encoding="utf-8")


def test_empty_set_delta_stays_overridden_unlike_clear() -> None:
    baseline = harness_profile_store.create_profile({"id": "empty.delta", "name": "Empty Delta"})
    set_result = harness_profile_store.apply_override_patch(
        "empty.delta",
        [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": [], "remove": []}}],
    )
    assert set_result["overrides"]["disabled_builtin_tools"] == {"add": [], "remove": []}
    assert set_result["revision"] != baseline["revision"]
    cleared = harness_profile_store.apply_override_patch(
        "empty.delta",
        [{"path": ["disabled_builtin_tools"], "op": "clear"}],
    )
    assert "disabled_builtin_tools" not in cleared["overrides"]
    assert cleared["revision"] != set_result["revision"]


def test_rejects_invalid_delta_shape() -> None:
    harness_profile_store.create_profile({"id": "bad.harness", "name": "Bad"})
    try:
        harness_profile_store.apply_override_patch(
            "bad.harness",
            [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": "not-a-list"}}],
        )
    except harness_profile_store.HarnessProfileError:
        pass
    else:
        raise AssertionError("invalid delta shape was accepted")


def test_stale_revision_rejected() -> None:
    profile = harness_profile_store.create_profile({"id": "stale.harness", "name": "Stale"})
    harness_profile_store.apply_override_patch(
        "stale.harness",
        [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": ["mssg"], "remove": []}}],
    )
    try:
        harness_profile_store.apply_override_patch(
            "stale.harness",
            [{"path": ["disabled_builtin_tools"], "op": "clear"}],
            revision=profile["revision"],
        )
    except harness_profile_store.HarnessProfileError:
        pass
    else:
        raise AssertionError("stale revision patch was accepted")


def main() -> int:
    try:
        test_create_profile_has_empty_overrides()
        test_create_profile_ignores_client_sent_overrides()
        test_overrides_delta_add_remove_roundtrip()
        test_default_is_not_a_stored_profile()
        test_delete_default_rejected()
        test_create_default_rejected()
        test_schema_version_2_mismatch_raises()
        test_empty_set_delta_stays_overridden_unlike_clear()
        test_rejects_invalid_delta_shape()
        test_stale_revision_rejected()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS harness profile store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
