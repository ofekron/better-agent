"""Unit coverage for lifecycle_state_store.py.

JSON state projection for session lifecycle (turn / prompt / execution /
admission / steer states), version 2 schema with a v1->v2 migration, strict
structural validation, and merge/save/load over ``<ba_home>/lifecycle-state.json``.
Every branch is pinned with a real assertion against the isolated test home
(no production state touched).

Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_lifecycle_state_store_unit.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _test_home  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_HOME = _test_home.isolate("bc-test-lifecycle-state-store-unit-")

import lifecycle_state_store as store  # noqa: E402
from paths import ba_home  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _path() -> Path:
    return ba_home() / "lifecycle-state.json"


def _write_raw(payload: object) -> None:
    """Write an arbitrary JSON payload (skipping store validation) to disk."""
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(payload))


def _turn(**overrides) -> dict:
    """A structurally valid idle turn with all required v2 fields."""
    base = {
        "state": "idle",
        "user_turn_id": None,
        "lifecycle_message_id": None,
        "execution_turn_id": None,
        "assistant_message_id": None,
        "executions": {},
        "admissions": {},
        "steers": {},
    }
    base.update(overrides)
    return base


def _running_turn() -> dict:
    """A valid running turn carrying a complete identity quad."""
    return _turn(
        state="running",
        user_turn_id="u1",
        lifecycle_message_id="m1",
        execution_turn_id="e1",
        assistant_message_id="a1",
    )


def _projection(**sessions) -> dict:
    return {"version": 2, "sessions": dict(sessions)}


def _valid_projection() -> dict:
    return _projection(s1={"turn": _turn()})


def _v1(sessions: dict) -> dict:
    return {"version": 1, "sessions": sessions}


def _v1_session(turn: dict | None = None, admissions: dict | None = None) -> dict:
    session: dict = {}
    if turn is not None:
        session["turn"] = dict(turn)
    if admissions is not None and isinstance(session.get("turn"), dict):
        session["turn"]["admissions"] = admissions
    return session


def _assert_invalid(fn, *args, **kwargs):
    """A store validation error must surface as RuntimeError."""
    try:
        fn(*args, **kwargs)
    except RuntimeError:
        return
    raise AssertionError(f"expected RuntimeError from {fn.__name__}")


# --------------------------------------------------------------------------- #
# _optional_id
# --------------------------------------------------------------------------- #


def test_optional_id_none():
    assert store._optional_id(None) is True


def test_optional_id_nonempty_str():
    assert store._optional_id("x") is True


def test_optional_id_empty_str():
    assert store._optional_id("") is False


def test_optional_id_non_str_types():
    assert store._optional_id(0) is False
    assert store._optional_id(False) is False
    assert store._optional_id([]) is False


# --------------------------------------------------------------------------- #
# _validate_state_map
# --------------------------------------------------------------------------- #


def test_state_map_not_dict():
    _assert_invalid(store._validate_state_map, [], {"a"}, "x")
    _assert_invalid(store._validate_state_map, None, {"a"}, "x")


def test_state_map_key_not_str():
    _assert_invalid(store._validate_state_map, {1: {"state": "a"}}, {"a"}, "x")


def test_state_map_value_not_dict():
    _assert_invalid(store._validate_state_map, {"k": "v"}, {"a"}, "x")


def test_state_map_state_not_allowed():
    _assert_invalid(store._validate_state_map, {"k": {"state": "z"}}, {"a"}, "x")


def test_state_map_happy():
    store._validate_state_map({"k": {"state": "a"}}, {"a"}, "x")
    store._validate_state_map({}, {"a"}, "x")


# --------------------------------------------------------------------------- #
# _validate_v2 — structural shell
# --------------------------------------------------------------------------- #


def test_validate_v2_sessions_not_dict():
    _assert_invalid(store._validate_v2, {"version": 2, "sessions": []})


def test_validate_v2_session_id_not_str():
    _assert_invalid(store._validate_v2, {"version": 2, "sessions": {1: {"turn": _turn()}}})


def test_validate_v2_session_not_dict():
    _assert_invalid(store._validate_v2, {"version": 2, "sessions": {"s1": "x"}})


def test_validate_v2_prompts_invalid_state():
    proj = _projection(s1={"turn": _turn(), "prompts": {"p": {"state": "bogus"}}})
    _assert_invalid(store._validate_v2, proj)


def test_validate_v2_prompts_happy():
    proj = _projection(s1={"turn": _turn(), "prompts": {"p": {"state": "sent"}}})
    store._validate_v2(proj)


# --------------------------------------------------------------------------- #
# _validate_v2 — turn validation
# --------------------------------------------------------------------------- #


def test_validate_v2_turn_missing():
    _assert_invalid(store._validate_v2, _projection(s1={}))


def test_validate_v2_turn_not_dict():
    _assert_invalid(store._validate_v2, _projection(s1={"turn": "x"}))


def test_validate_v2_turn_bad_state():
    _assert_invalid(store._validate_v2, _projection(s1={"turn": {"state": "bogus"}}))


def test_validate_v2_turn_identity_empty_str():
    turn = _turn(user_turn_id="")
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_turn_identity_non_str():
    turn = _turn(assistant_message_id=5)
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_running_requires_complete_identity():
    turn = _turn(state="running", user_turn_id=None,
                 lifecycle_message_id="m1", execution_turn_id="e1",
                 assistant_message_id="a1")
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_legacy_cannot_carry_identity():
    turn = _turn(state="legacy_reconciling", user_turn_id="u1")
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_running_complete_identity_ok():
    store._validate_v2(_projection(s1={"turn": _running_turn()}))


def test_validate_v2_legacy_all_none_ok():
    store._validate_v2(_projection(s1={"turn": _turn(state="legacy_reconciling")}))


# --------------------------------------------------------------------------- #
# _validate_v2 — executions
# --------------------------------------------------------------------------- #


def test_validate_v2_execution_missing_assistant_identity_is_valid():
    # assistant_message_id defaults to None, which _optional_id accepts.
    turn = _turn(executions={"e1": {"state": "complete"}})
    store._validate_v2(_projection(s1={"turn": turn}))


def test_validate_v2_execution_assistant_empty():
    turn = _turn(executions={"e1": {"state": "complete", "assistant_message_id": ""}})
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_execution_not_running_for_other_id():
    # execution_turn_id is e1, but a different execution e2 is running -> reject
    turn = _running_turn()
    turn["executions"] = {"e2": {"state": "running", "assistant_message_id": "a2"}}
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_execution_running_matches_turn_ok():
    turn = _running_turn()
    turn["executions"] = {"e1": {"state": "running", "assistant_message_id": "a1"}}
    store._validate_v2(_projection(s1={"turn": turn}))


def test_validate_v2_execution_other_complete_ok():
    turn = _running_turn()
    turn["executions"] = {
        "e1": {"state": "running", "assistant_message_id": "a1"},
        "e2": {"state": "complete", "assistant_message_id": "a2"},
    }
    store._validate_v2(_projection(s1={"turn": turn}))


def test_validate_v2_execution_bad_state():
    turn = _turn(executions={"e1": {"state": "bogus", "assistant_message_id": "a1"}})
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_execution_value_not_dict():
    turn = _turn(executions={"e1": "x"})
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


# --------------------------------------------------------------------------- #
# _validate_v2 — admissions
# --------------------------------------------------------------------------- #


def _admission(**overrides) -> dict:
    base = {
        "state": "admitted",
        "execution_turn_id": "e1",
        "user_turn_id": "u1",
        "lifecycle_message_id": "m1",
        "assistant_message_id": "a1",
    }
    base.update(overrides)
    return base


def test_validate_v2_admission_identity_empty():
    turn = _running_turn()
    turn["admissions"] = {"r1": _admission(user_turn_id="")}
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_admission_parent_mismatch():
    turn = _running_turn()
    turn["admissions"] = {"r1": _admission(user_turn_id="other")}
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_admission_matches_parent_ok():
    turn = _running_turn()
    turn["admissions"] = {"r1": _admission()}
    store._validate_v2(_projection(s1={"turn": turn}))


def test_validate_v2_admission_bad_state():
    turn = _running_turn()
    turn["admissions"] = {"r1": _admission(state="bogus")}
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_admission_value_not_dict():
    turn = _running_turn()
    turn["admissions"] = {"r1": "x"}
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_admission_legacy_skips_parent_match():
    # legacy turns carry no identity, so admission parent-match is skipped.
    turn = _turn(state="legacy_reconciling",
                 executions={}, admissions={"r1": {
                     "state": "admitted", "execution_turn_id": None,
                     "user_turn_id": None, "lifecycle_message_id": None,
                     "assistant_message_id": None}}, steers={})
    store._validate_v2(_projection(s1={"turn": turn}))


# --------------------------------------------------------------------------- #
# _validate_v2 — steers
# --------------------------------------------------------------------------- #


def test_validate_v2_steer_bad_state():
    turn = _turn(steers={"st1": {"state": "bogus"}})
    _assert_invalid(store._validate_v2, _projection(s1={"turn": turn}))


def test_validate_v2_steer_happy():
    turn = _turn(steers={"st1": {"state": "accepted"}})
    store._validate_v2(_projection(s1={"turn": turn}))


# --------------------------------------------------------------------------- #
# _migrate_v1_to_v2
# --------------------------------------------------------------------------- #


def test_migrate_sessions_not_dict():
    _assert_invalid(store._migrate_v1_to_v2, {"sessions": []})


def test_migrate_session_id_not_str():
    _assert_invalid(store._migrate_v1_to_v2, {"sessions": {1: {"turn": {}}}})


def test_migrate_session_value_not_dict():
    _assert_invalid(store._migrate_v1_to_v2, {"sessions": {"s1": "x"}})


def test_migrate_running_turn_becomes_legacy():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": {"state": "running"}}}})
    turn = out["sessions"]["s1"]["turn"]
    assert out["version"] == 2
    assert turn["state"] == "legacy_reconciling"
    # v2 identity fields default to None (legacy cannot carry identity)
    for key in ("user_turn_id", "lifecycle_message_id",
                "execution_turn_id", "assistant_message_id"):
        assert turn[key] is None
    assert turn["executions"] == {}
    assert turn["admissions"] == {}
    assert turn["steers"] == {}


def test_migrate_non_running_turn_preserves_state():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": {"state": "idle"}}}})
    assert out["sessions"]["s1"]["turn"]["state"] == "idle"


def test_migrate_turn_not_dict_yields_empty_turn():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": "x"}}})
    turn = out["sessions"]["s1"]["turn"]
    # a non-dict turn becomes {} then only the v2 default keys are set; no state.
    assert "state" not in turn
    assert turn["executions"] == {}
    assert turn["admissions"] == {}


def test_migrate_no_turn_key():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {}}})
    turn = out["sessions"]["s1"]["turn"]
    assert "state" not in turn
    assert turn["steers"] == {}


def test_migrate_admission_turn_run_id_renamed():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": {
        "state": "idle",
        "admissions": {"r1": {"state": "admitted", "turn_run_id": "e9"}},
    }}}})
    adm = out["sessions"]["s1"]["turn"]["admissions"]["r1"]
    assert adm["execution_turn_id"] == "e9"
    assert "turn_run_id" not in adm
    for key in ("user_turn_id", "lifecycle_message_id", "assistant_message_id"):
        assert adm[key] is None


def test_migrate_admission_no_turn_run_id_defaults_none():
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": {
        "state": "idle",
        "admissions": {"r1": {"state": "admitted"}},
    }}}})
    assert out["sessions"]["s1"]["turn"]["admissions"]["r1"]["execution_turn_id"] is None


def test_migrate_admission_run_id_not_str():
    payload = {"sessions": {"s1": {"turn": {
        "state": "idle", "admissions": {1: {"state": "admitted"}}}}}}
    _assert_invalid(store._migrate_v1_to_v2, payload)


def test_migrate_admission_value_not_dict():
    payload = {"sessions": {"s1": {"turn": {
        "state": "idle", "admissions": {"r1": "x"}}}}}
    _assert_invalid(store._migrate_v1_to_v2, payload)


def test_migrate_admissions_not_dict_left_alone():
    # non-dict admissions block is skipped (isinstance guard), turn keeps it
    out = store._migrate_v1_to_v2({"sessions": {"s1": {"turn": {
        "state": "idle", "admissions": "x"}}}})
    assert out["sessions"]["s1"]["turn"]["admissions"] == "x"


# --------------------------------------------------------------------------- #
# load()
# --------------------------------------------------------------------------- #


def test_load_missing_file_returns_default():
    assert _path().exists() is False
    assert store.load() == {"version": 2, "sessions": {}}


def test_load_empty_file_returns_default():
    _write_raw({})
    assert store.load() == {"version": 2, "sessions": {}}


def test_load_invalid_version_not_int():
    _write_raw({"version": "2", "sessions": {}})
    _assert_invalid(store.load)


def test_load_unsupported_future_version():
    _write_raw({"version": 99, "sessions": {}})
    _assert_invalid(store.load)


def test_load_valid_v2_passthrough():
    proj = _valid_projection()
    _write_raw(proj)
    assert store.load() == proj


def test_load_migrates_v1_and_rewrites():
    _write_raw(_v1(sessions={"s1": {"turn": {"state": "running"}}}))
    loaded = store.load()
    assert loaded["version"] == 2
    assert loaded["sessions"]["s1"]["turn"]["state"] == "legacy_reconciling"
    # migrated projection is rewritten to disk as v2
    on_disk = json.loads(_path().read_text())
    assert on_disk["version"] == 2
    assert on_disk["sessions"]["s1"]["turn"]["state"] == "legacy_reconciling"


def test_load_v1_sessions_not_dict():
    _write_raw({"version": 1, "sessions": []})
    _assert_invalid(store.load)


def test_load_v2_sessions_not_dict():
    _write_raw({"version": 2, "sessions": []})
    _assert_invalid(store.load)


def test_load_v2_invalid_structure():
    _write_raw({"version": 2, "sessions": {"s1": {"turn": {"state": "bogus"}}}})
    _assert_invalid(store.load)


def test_load_v1_invalid_structure_after_migration():
    # v1 turn with state running AND a pre-set identity field: migration flips
    # state to legacy_reconciling but keeps the identity -> fails v2 validation.
    _write_raw({"version": 1, "sessions": {"s1": {"turn": {
        "state": "running", "user_turn_id": "u1"}}}})
    _assert_invalid(store.load)


def test_load_gap_migration_unsupported():
    # version 0 has no registered migration -> unsupported
    _write_raw({"version": 0, "sessions": {}})
    _assert_invalid(store.load)


# --------------------------------------------------------------------------- #
# save()
# --------------------------------------------------------------------------- #


def test_save_wrong_version_rejected():
    _assert_invalid(store.save, {"version": 1, "sessions": {}})


def test_save_invalid_structure_rejected():
    _assert_invalid(store.save, {"version": 2, "sessions": {"s1": {"turn": {"state": "x"}}}})


def test_save_writes_valid_projection():
    proj = _valid_projection()
    store.save(proj)
    assert json.loads(_path().read_text()) == proj


def test_save_overwrites_existing():
    store.save(_valid_projection())
    store.save(_projection(s2={"turn": _turn()}))
    on_disk = json.loads(_path().read_text())
    assert list(on_disk["sessions"].keys()) == ["s2"]


# --------------------------------------------------------------------------- #
# merge_sessions()
# --------------------------------------------------------------------------- #


def test_merge_adds_session():
    store.save(_projection())
    store.merge_sessions({"s1": {"turn": _turn()}})
    assert "s1" in store.load()["sessions"]


def test_merge_removes_session_via_none():
    store.save(_projection(s1={"turn": _turn()}))
    store.merge_sessions({"s1": None})
    assert store.load()["sessions"] == {}


def test_merge_remove_missing_session_noop():
    store.save(_projection())
    store.merge_sessions({"ghost": None})
    assert store.load()["sessions"] == {}


def test_merge_replaces_session():
    store.save(_projection(s1={"turn": _turn(state="idle")}))
    store.merge_sessions({"s1": {"turn": _running_turn()}})
    assert store.load()["sessions"]["s1"]["turn"]["state"] == "running"


def test_merge_rejects_invalid_via_save():
    store.save(_projection())
    _assert_invalid(store.merge_sessions, {"s1": {"turn": {"state": "bogus"}}})


# --------------------------------------------------------------------------- #
# _path
# --------------------------------------------------------------------------- #


def test_path_under_home():
    assert store._path() == ba_home() / "lifecycle-state.json"
