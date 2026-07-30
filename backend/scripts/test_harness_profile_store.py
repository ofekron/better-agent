from __future__ import annotations

import os
import shutil
import sqlite3
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
import harness_profile_resolver


def test_create_profile_has_empty_overrides() -> None:
    profile = harness_profile_store.create_profile({
        "id": "personal.harness",
        "name": "Personal Harness",
        "description": "hand-tuned overrides",
    })
    assert profile["schema_version"] == 5
    assert profile["overrides"] == {}
    assert profile["base_profile_id"] is None
    assert profile["default_model"] is None
    assert profile["provisioning_prompt"] is None
    loaded = harness_profile_store.get_profile("personal.harness")
    assert loaded == profile


def test_create_profile_derives_id_from_name_when_id_omitted() -> None:
    # Regression: the frontend's "create profile" form only sends {name}
    # (matching normal create-by-name UX, no id field to fill in). Every
    # existing test before this one always supplied an explicit id, which
    # is why create_profile requiring id and never deriving one from name
    # went unnoticed and broke every real "create new profile" click with
    # a 400 "id is required".
    profile = harness_profile_store.create_profile({"name": "My Client Demo Profile"})
    assert profile["id"] == "my-client-demo-profile"
    assert profile["name"] == "My Client Demo Profile"
    assert profile["overrides"] == {}


def test_create_profile_disambiguates_id_collision_from_same_name() -> None:
    first = harness_profile_store.create_profile({"name": "Duplicate Name"})
    second = harness_profile_store.create_profile({"name": "Duplicate Name"})
    assert first["id"] != second["id"]
    assert second["id"] == "duplicate-name-2"


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
                "path": ["disabled_runtime_skills"],
                "op": "set",
                "value": {"add": ["*"], "remove": []},
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
    assert updated["overrides"]["disabled_runtime_skills"] == {"add": ["*"], "remove": []}
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
    # Force-touch the store first so the sqlite file + meta row exist, then
    # corrupt the recorded schema_version directly to simulate an
    # older-version store on disk.
    harness_profile_store._load()
    conn = harness_profile_store._connect()
    try:
        with conn:
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    finally:
        conn.close()
    try:
        harness_profile_store.list_profiles()
    except harness_profile_store.HarnessProfileError:
        pass
    else:
        raise AssertionError("v1-shaped store should have raised on load")
    finally:
        # Reset to the current schema version for any tests that run after this one.
        conn = sqlite3.connect(str(harness_profile_store._db_path()))
        try:
            with conn:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(harness_profile_store.SCHEMA_VERSION),),
                )
        finally:
            conn.close()


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


def test_set_profile_meta_roundtrip() -> None:
    harness_profile_store.create_profile({"id": "meta.base", "name": "Meta Base"})
    child = harness_profile_store.create_profile({"id": "meta.child", "name": "Meta Child"})
    updated = harness_profile_store.set_profile_meta(
        "meta.child",
        {
            "base_profile_id": "meta.base",
            "default_provider_id": "codex",
            "default_model": "gpt-5.5",
            "default_reasoning_effort": "high",
            "provisioning_prompt": "Prepare a strict reviewer workspace.",
        },
        revision=child["revision"],
    )
    assert updated["base_profile_id"] == "meta.base"
    assert updated["default_provider_id"] == "codex"
    assert updated["default_model"] == "gpt-5.5"
    assert updated["default_reasoning_effort"] == "high"
    assert updated["provisioning_prompt"] == "Prepare a strict reviewer workspace."
    loaded = harness_profile_store.get_profile("meta.child")
    assert loaded["base_profile_id"] == "meta.base"


def test_provisioning_prompt_inherits_from_base_profile() -> None:
    base = harness_profile_store.create_profile({"id": "prompt.base", "name": "Prompt Base"})
    harness_profile_store.set_profile_meta(
        "prompt.base",
        {"provisioning_prompt": "Base worker prep"},
        revision=base["revision"],
    )
    harness_profile_store.create_profile({"id": "prompt.child", "name": "Prompt Child"})
    harness_profile_store.set_profile_meta("prompt.child", {"base_profile_id": "prompt.base"})
    resolved = harness_profile_resolver.resolve_profile("prompt.child")
    assert resolved["provisioning_prompt"] == "Base worker prep"


def test_set_profile_meta_rejects_unknown_field() -> None:
    harness_profile_store.create_profile({"id": "meta.unknown", "name": "Meta Unknown"})
    try:
        harness_profile_store.set_profile_meta("meta.unknown", {"bogus": "x"})
    except harness_profile_store.HarnessProfileError:
        pass
    else:
        raise AssertionError("unknown meta field was accepted")


def test_base_self_reference_rejected() -> None:
    harness_profile_store.create_profile({"id": "cycle.self", "name": "Cycle Self"})
    try:
        harness_profile_store.set_profile_meta("cycle.self", {"base_profile_id": "cycle.self"})
    except harness_profile_store.HarnessProfileError as exc:
        assert "itself" in str(exc) or "cycle" in str(exc)
    else:
        raise AssertionError("A->A base self-reference was accepted")


def test_indirect_base_cycle_rejected_at_save() -> None:
    harness_profile_store.create_profile({"id": "cycle.a", "name": "Cycle A"})
    harness_profile_store.create_profile({"id": "cycle.b", "name": "Cycle B"})
    # B bases on A (fine), then setting A's base to B would close A->B->A.
    harness_profile_store.set_profile_meta("cycle.b", {"base_profile_id": "cycle.a"})
    try:
        harness_profile_store.set_profile_meta("cycle.a", {"base_profile_id": "cycle.b"})
    except harness_profile_store.HarnessProfileError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("A->B->A base cycle was accepted at save time")


def test_base_missing_profile_rejected() -> None:
    harness_profile_store.create_profile({"id": "base.missing", "name": "Base Missing"})
    try:
        harness_profile_store.set_profile_meta("base.missing", {"base_profile_id": "does-not-exist"})
    except harness_profile_store.HarnessProfileError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("base pointing at a missing profile was accepted")


def test_base_bad_revision_rejected() -> None:
    harness_profile_store.create_profile({"id": "base.rev.target", "name": "Rev Target"})
    harness_profile_store.create_profile({"id": "base.rev.child", "name": "Rev Child"})
    try:
        harness_profile_store.set_profile_meta(
            "base.rev.child",
            {"base_profile_id": "base.rev.target", "base_profile_revision": "deadbeefdeadbeef"},
        )
    except harness_profile_store.HarnessProfileError as exc:
        assert "revision unavailable" in str(exc)
    else:
        raise AssertionError("base pinned to a nonexistent revision was accepted")


def test_extension_owned_profile_is_read_only() -> None:
    original = harness_profile_store._extension_profiles
    harness_profile_store._extension_profiles = lambda: [{
        "id": "private.reviewer",
        "schema_version": harness_profile_store.SCHEMA_VERSION,
        "name": "Private Reviewer",
        "description": "",
        "source": "extension",
        "extension_id": "private.adv",
        "read_only": True,
        "overrides": {"disabled_runtime_skills": {"add": ["*"], "remove": []}},
        "mcp_overrides": {},
        "skill_overrides": {},
        "native_harness_overrides": {},
        "provider_run_config_overlay": {},
        "capability_contexts": [],
        "secret_refs": {},
        "base_profile_id": None,
        "base_profile_revision": None,
        "default_provider_id": None,
        "default_model": None,
        "default_reasoning_effort": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "revision": "fixture",
    }]
    try:
        profile = harness_profile_store.get_profile("private.reviewer")
        assert profile["read_only"] is True
        assert profile["overrides"]["disabled_runtime_skills"] == {"add": ["*"], "remove": []}
        try:
            harness_profile_store.create_profile({"id": "private.reviewer", "name": "Duplicate"})
        except harness_profile_store.HarnessProfileError as exc:
            assert "owned by an extension" in str(exc)
        else:
            raise AssertionError("extension-owned profile id was overwritten")
    finally:
        harness_profile_store._extension_profiles = original


def test_store_fingerprint_bumps_on_write_and_delete() -> None:
    before = harness_profile_store.store_fingerprint()
    created = harness_profile_store.create_profile({"id": "fp.one", "name": "FP One"})
    after_create = harness_profile_store.store_fingerprint()
    assert after_create != before
    harness_profile_store.apply_override_patch(
        "fp.one",
        [{"path": ["disabled_builtin_tools"], "op": "set", "value": {"add": ["mssg"], "remove": []}}],
        revision=created["revision"],
    )
    after_update = harness_profile_store.store_fingerprint()
    assert after_update != after_create
    harness_profile_store.delete_profile("fp.one")
    after_delete = harness_profile_store.store_fingerprint()
    assert after_delete != after_update


def test_write_touches_only_its_own_row() -> None:
    # A write to one profile must not rewrite unrelated rows: their
    # `updated_at` should be untouched by a sibling's write.
    harness_profile_store.create_profile({"id": "row.a", "name": "Row A"})
    untouched = harness_profile_store.create_profile({"id": "row.b", "name": "Row B"})
    harness_profile_store.set_profile_meta("row.a", {"default_model": "gpt-5.5"})
    reloaded_b = harness_profile_store.get_profile("row.b")
    assert reloaded_b["updated_at"] == untouched["updated_at"]
    assert reloaded_b["revision"] == untouched["revision"]


def test_concurrent_reads_see_committed_writes_under_wal() -> None:
    # WAL mode: a second connection opened after a committed write must
    # observe it (this exercises the actual on-disk WAL/commit path, not
    # just the in-process dict).
    harness_profile_store.create_profile({"id": "wal.one", "name": "WAL One"})
    conn = sqlite3.connect(str(harness_profile_store._db_path()))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        row = conn.execute("SELECT id FROM profiles WHERE id = 'wal.one'").fetchone()
        assert row is not None
    finally:
        conn.close()


def main() -> int:
    try:
        test_create_profile_has_empty_overrides()
        test_create_profile_derives_id_from_name_when_id_omitted()
        test_create_profile_disambiguates_id_collision_from_same_name()
        test_create_profile_ignores_client_sent_overrides()
        test_overrides_delta_add_remove_roundtrip()
        test_default_is_not_a_stored_profile()
        test_delete_default_rejected()
        test_create_default_rejected()
        test_schema_version_2_mismatch_raises()
        test_empty_set_delta_stays_overridden_unlike_clear()
        test_rejects_invalid_delta_shape()
        test_stale_revision_rejected()
        test_set_profile_meta_roundtrip()
        test_provisioning_prompt_inherits_from_base_profile()
        test_set_profile_meta_rejects_unknown_field()
        test_base_self_reference_rejected()
        test_indirect_base_cycle_rejected_at_save()
        test_base_missing_profile_rejected()
        test_base_bad_revision_rejected()
        test_extension_owned_profile_is_read_only()
        test_store_fingerprint_bumps_on_write_and_delete()
        test_write_touches_only_its_own_row()
        test_concurrent_reads_see_committed_writes_under_wal()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS harness profile store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
