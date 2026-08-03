from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lifecycle_command_store as store  # noqa: E402
from lifecycle_command_model import (  # noqa: E402
    ExecutionTurnIdentity,
    LifecycleCommand,
    LifecycleSnapshot,
    UserTurnIdentity,
)
from lifecycle_command_states import STATES  # noqa: E402
from process_identity import ProcessIdentity  # noqa: E402


def _reset_store() -> None:
    """Initialize whatever home is currently active and empty every table.

    The active test home is process-global (see paths.engage_test_home): the
    last ``isolate()`` caller wins for the whole pytest run. Rather than fight
    that, we (a) never call ``isolate()`` ourselves so we never hijack another
    file's home, and (b) re-initialize + wipe the active home around each test
    so this module is correct no matter which home is live.
    """
    store.initialize()
    with store.connection() as database:
        database.execute("DELETE FROM pending_terminal_renders")
        database.execute("DELETE FROM effects")
        database.execute("DELETE FROM transitions")
        database.execute("DELETE FROM sessions")
        database.execute("DELETE FROM authority_owner")
        database.commit()


@pytest.fixture(autouse=True)
def _isolated_store() -> Any:
    _reset_store()
    yield
    _reset_store()


def _identity(suffix: str) -> UserTurnIdentity:
    return UserTurnIdentity(
        user_turn_id=f"user-{suffix}",
        lifecycle_message_id=f"message-{suffix}",
    )


def _begin_command(
    session_id: str,
    request_id: str,
    suffix: str,
    policy: str = "single",
) -> LifecycleCommand:
    return LifecycleCommand(
        request_id=request_id,
        session_id=session_id,
        kind="begin_turn",
        identity=_identity(suffix),
        execution_policy=policy,
    )


def _persist_begin(
    session_id: str,
    request_id: str,
    suffix: str,
    policy: str = "single",
) -> tuple[LifecycleCommand, LifecycleSnapshot]:
    command = _begin_command(session_id, request_id, suffix, policy)
    plan = STATES["idle"].decide(LifecycleSnapshot(), command)
    assert store.persist_plan(command, LifecycleSnapshot(), plan) == "inserted"
    return command, plan.next_snapshot


def _finish_begin(session_id: str, request_id: str, suffix: str) -> LifecycleSnapshot:
    _persist_begin(session_id, request_id, suffix)
    store.record_effect_result(session_id, request_id, 0, {"observed": "turn_begin"})
    return store.commit_transition(session_id, request_id)


# --------------------------------------------------------------------------- #
# initialization + connection
# --------------------------------------------------------------------------- #

def test_initialize_is_idempotent() -> None:
    path = store.store_path()
    assert path in store._INITIALIZED_PATHS
    store.initialize()
    assert path in store._INITIALIZED_PATHS


def test_connection_requires_initialization() -> None:
    saved = set(store._INITIALIZED_PATHS)
    store._INITIALIZED_PATHS.clear()
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            with store.connection():
                pass
    finally:
        store._INITIALIZED_PATHS.update(saved)


def test_open_connection_count_tracks_context() -> None:
    assert store.open_connection_count() == 0
    with store.connection():
        assert store.open_connection_count() == 1
    assert store.open_connection_count() == 0


def test_store_path_under_home() -> None:
    assert store.store_path().name == "lifecycle-command-state.sqlite3"


# --------------------------------------------------------------------------- #
# migration guards
# --------------------------------------------------------------------------- #

def test_migrate_rejects_negative_schema_version() -> None:
    database = sqlite3.connect(":memory:")
    database.execute("PRAGMA user_version = -1")
    try:
        with pytest.raises(RuntimeError, match="invalid lifecycle command schema version"):
            store._migrate(database)
    finally:
        database.close()


def test_migrate_rejects_unsupported_schema_version() -> None:
    database = sqlite3.connect(":memory:")
    database.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    try:
        with pytest.raises(RuntimeError, match="unsupported lifecycle command schema"):
            store._migrate(database)
    finally:
        database.close()


def test_logical_identity_rejects_non_dict() -> None:
    with pytest.raises(RuntimeError, match="invalid persisted turn identity"):
        store._logical_identity("not-a-dict")


def test_migrate_is_noop_on_current_schema() -> None:
    # The store DB is already at SCHEMA_VERSION. Re-running _migrate must be a
    # no-op; this exercises the migration fall-through for an already-current
    # schema (the version != 0 and version != 1 path through _migrate).
    with store.connection() as database:
        store._migrate(database)
    # Schema is intact and usable.
    _persist_begin("noop-migrate-sess", "req-noop", "noop")


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_V1_SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    identity_json TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 0)
) STRICT;
CREATE TABLE transitions (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    command_json TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    next_snapshot_json TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    notification_payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (session_id, request_id)
) STRICT;
CREATE TABLE effects (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    effect_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    PRIMARY KEY (session_id, request_id, ordinal),
    UNIQUE (effect_id)
) STRICT;
CREATE UNIQUE INDEX one_unfinished_transition_per_session
    ON transitions(session_id)
    WHERE status IN ('planned', 'effects_applied', 'committed');
CREATE INDEX transitions_by_status
    ON transitions(status, session_id, request_id);
PRAGMA user_version = 1;
"""


def test_migrate_v1_snapshot_without_identity(tmp_path: Path) -> None:
    # A legacy v1 transition bound for idle carries a next_snapshot with no
    # identity. The v1->v2 migration must leave it identity-less rather than
    # fabricating one (the next_snapshot.get("identity") is None branch).
    database = sqlite3.connect(tmp_path / "legacy.sqlite3")
    database.row_factory = sqlite3.Row
    database.executescript(_V1_SCHEMA)
    old_identity = {
        "user_turn_id": "u-v1",
        "lifecycle_message_id": "m-v1",
        "execution_turn_id": "e-v1",
        "assistant_message_id": "a-v1",
    }
    command = {
        "request_id": "req-v1",
        "session_id": "sess-v1",
        "kind": "finish_turn",
        "identity": old_identity,
        "outcome": "stopped",
    }
    next_snapshot = {"phase": "idle", "revision": 1}
    notification = {
        "request_id": "req-v1",
        "command": "finish_turn",
        "source_phase": "stopping",
        "next_phase": "idle",
        "identity": old_identity,
    }
    database.execute(
        "INSERT INTO sessions VALUES ('sess-v1', 'idle', NULL, 1)",
    )
    database.execute(
        "INSERT INTO transitions VALUES (?, ?, ?, ?, 0, ?, "
        "'lifecycle_command_completed', ?, 'notification_attempted')",
        (
            "sess-v1",
            "req-v1",
            _encode(command),
            _encode(command),
            _encode(next_snapshot),
            _encode(notification),
        ),
    )
    database.commit()

    store._migrate(database)

    migrated = database.execute(
        "SELECT next_snapshot_json FROM transitions WHERE session_id = ?",
        ("sess-v1",),
    ).fetchone()
    snapshot = json.loads(migrated["next_snapshot_json"])
    # The idle-bound snapshot stays identity-less through migration.
    assert snapshot.get("identity") is None
    assert snapshot["execution"] is None
    assert database.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    database.close()


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #

def test_session_snapshot_missing_returns_default() -> None:
    assert store.session_snapshot("missing-session").phase == "idle"


def test_active_session_snapshots_excludes_idle() -> None:
    store.align_session_owner("idle-sess", "inc-1")
    _finish_begin("active-sess", "req-active", "active")
    actives = dict(store.active_session_snapshots())
    assert "active-sess" in actives
    assert actives["active-sess"].phase == "starting"
    assert "idle-sess" not in actives


# --------------------------------------------------------------------------- #
# align_session_owner
# --------------------------------------------------------------------------- #

def test_align_session_owner_inserts_new_row() -> None:
    store.align_session_owner("align-new", "inc-new")
    row = store.session_snapshot("align-new")
    assert row.phase == "idle"
    with store.connection() as database:
        owner = database.execute(
            "SELECT owner_incarnation FROM sessions WHERE session_id = ?",
            ("align-new",),
        ).fetchone()
    assert owner["owner_incarnation"] == "inc-new"


def test_align_session_owner_updates_null_owner() -> None:
    # persist_plan creates a session row with a NULL owner_incarnation.
    _persist_begin("align-null", "req-null", "null")
    store.align_session_owner("align-null", "inc-bound")
    with store.connection() as database:
        owner = database.execute(
            "SELECT owner_incarnation FROM sessions WHERE session_id = ?",
            ("align-null",),
        ).fetchone()
    assert owner["owner_incarnation"] == "inc-bound"


def test_align_session_owner_replaces_on_incarnation_mismatch() -> None:
    store.align_session_owner("align-replace", "inc-old")
    _persist_begin("align-replace", "req-old", "replace")
    assert store.transition_for("align-replace", "req-old") is not None
    store.align_session_owner("align-replace", "inc-new")
    # The mismatch path deletes prior rows and re-creates an idle session.
    assert store.transition_for("align-replace", "req-old") is None
    assert store.session_snapshot("align-replace").phase == "idle"


def test_align_session_owner_noop_on_matching_incarnation() -> None:
    store.align_session_owner("align-match", "inc-same")
    store.align_session_owner("align-match", "inc-same")
    with store.connection() as database:
        owner = database.execute(
            "SELECT owner_incarnation FROM sessions WHERE session_id = ?",
            ("align-match",),
        ).fetchone()
    assert owner["owner_incarnation"] == "inc-same"


# --------------------------------------------------------------------------- #
# authority
# --------------------------------------------------------------------------- #

def _clear_authority() -> None:
    with store.connection() as database:
        database.execute("DELETE FROM authority_owner")
        database.commit()


def _live_identity() -> ProcessIdentity:
    return ProcessIdentity(
        pid=os.getpid(),
        create_time=float(psutil.Process().create_time()),
    )


def test_acquire_authority_rejects_invalid_pid() -> None:
    _clear_authority()
    with pytest.raises(ValueError, match="positive integer"):
        store.acquire_authority("owner", ProcessIdentity(pid=0, create_time=1.0))


def test_acquire_authority_rejects_non_finite_create_time() -> None:
    _clear_authority()
    with pytest.raises(ValueError, match="finite"):
        store.acquire_authority("owner", ProcessIdentity(pid=1, create_time=math.inf))


def test_acquire_authority_is_idempotent_for_same_owner() -> None:
    _clear_authority()
    identity = _live_identity()
    store.acquire_authority("owner-a", identity)
    store.acquire_authority("owner-a", identity)
    owner = store.authority_owner()
    assert owner is not None
    assert owner["owner_id"] == "owner-a"


def test_acquire_authority_blocks_live_competitor() -> None:
    _clear_authority()
    live = _live_identity()
    store.acquire_authority("owner-a", live)
    with pytest.raises(store.AuthorityLeaseHeld, match="owned by a live"):
        store.acquire_authority("owner-b", live)


def test_acquire_authority_takes_over_proven_dead_owner() -> None:
    _clear_authority()
    # Seed a lease for a pid that does not exist anywhere on the machine.
    dead_pid = 0x7FFFFFFF
    with store.connection() as database:
        database.execute(
            "INSERT INTO authority_owner(singleton, owner_id, pid, create_time) "
            "VALUES (1, ?, ?, ?)",
            ("owner-dead", dead_pid, 1.0),
        )
        database.commit()
    live = _live_identity()
    store.acquire_authority("owner-new", live)
    owner = store.authority_owner()
    assert owner is not None
    assert owner["owner_id"] == "owner-new"
    assert owner["pid"] == live.pid


def test_release_authority_and_authority_owner() -> None:
    _clear_authority()
    live = _live_identity()
    store.acquire_authority("owner-a", live)
    assert store.authority_owner() is not None
    # A non-matching identity does not release the lease.
    store.release_authority("owner-a", ProcessIdentity(pid=live.pid, create_time=0.0))
    assert store.authority_owner() is not None
    store.release_authority("owner-a", live)
    assert store.authority_owner() is None


# --------------------------------------------------------------------------- #
# persist_plan
# --------------------------------------------------------------------------- #

def test_persist_plan_returns_existing_for_duplicate_fingerprint() -> None:
    command, _ = _persist_begin("dup-sess", "req-dup", "dup")
    plan = STATES["idle"].decide(LifecycleSnapshot(), command)
    assert store.persist_plan(command, LifecycleSnapshot(), plan) == "existing"


def test_persist_plan_conflict_on_divergent_command() -> None:
    _persist_begin("conflict-sess", "req-conflict", "first")
    other = _begin_command("conflict-sess", "req-conflict", "second")
    plan = STATES["idle"].decide(LifecycleSnapshot(), other)
    with pytest.raises(store.TransitionConflict, match="already bound"):
        store.persist_plan(other, LifecycleSnapshot(), plan)


def test_persist_plan_rejects_changed_snapshot() -> None:
    _finish_begin("stale-sess", "req-stale-1", "stale")
    command = _begin_command("stale-sess", "req-stale-2", "stale")
    plan = STATES["idle"].decide(LifecycleSnapshot(), command)
    with pytest.raises(store.SnapshotChanged, match="changed before intent"):
        store.persist_plan(command, LifecycleSnapshot(), plan)


def test_persist_plan_busy_on_unfinished_sibling() -> None:
    _persist_begin("busy-sess", "req-busy-1", "busy")
    command = _begin_command("busy-sess", "req-busy-2", "busy")
    plan = STATES["idle"].decide(LifecycleSnapshot(), command)
    with pytest.raises(store.TransitionBusy, match="unfinished"):
        store.persist_plan(command, LifecycleSnapshot(), plan)


# --------------------------------------------------------------------------- #
# transition reads
# --------------------------------------------------------------------------- #

def test_required_transition_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="disappeared"):
        store.required_transition("no-sess", "no-req")


def test_unfinished_transitions_tracks_status() -> None:
    _persist_begin("unfin-sess", "req-unfin", "unfin")
    assert ("unfin-sess", "req-unfin") in store.unfinished_transitions()
    store.record_effect_result("unfin-sess", "req-unfin", 0, {"o": 1})
    # 'committed' is still an unfinished status until notification is attempted.
    store.commit_transition("unfin-sess", "req-unfin")
    assert ("unfin-sess", "req-unfin") in store.unfinished_transitions()
    store.mark_notification_attempted("unfin-sess", "req-unfin")
    assert ("unfin-sess", "req-unfin") not in store.unfinished_transitions()


def test_required_transition_returns_existing() -> None:
    _persist_begin("req-sess", "req-existing", "req")
    transition = store.required_transition("req-sess", "req-existing")
    assert transition["status"] == "planned"


def test_transition_for_projects_recorded_effect_results() -> None:
    _persist_begin("res-sess", "req-res", "res")
    store.record_effect_result("res-sess", "req-res", 0, {"done": True})
    transition = store.transition_for("res-sess", "req-res")
    assert transition is not None
    assert {"done": True} in transition["effect_results"]


def test_logical_identity_projects_valid_dict() -> None:
    result = store._logical_identity(
        {"user_turn_id": "u", "lifecycle_message_id": "m", "extra": "dropped"},
    )
    assert result == {"user_turn_id": "u", "lifecycle_message_id": "m"}


def test_record_effect_result_keeps_transition_planned_with_remaining_effects() -> None:
    _persist_begin("multi-sess", "req-multi", "multi")
    # Seed a second effect row so recording ordinal 0 leaves a remaining effect.
    with store.connection() as database:
        database.execute(
            "INSERT INTO effects(session_id, request_id, ordinal, effect_id, "
            "kind, payload_json) VALUES (?, ?, 1, 'extra-effect', "
            "'observe_turn_started', ?)",
            (
                "multi-sess",
                "req-multi",
                store._dump({"request_id": "req-multi"}),
            ),
        )
        database.commit()
    store.record_effect_result("multi-sess", "req-multi", 0, {"o": 1})
    transition = store.transition_for("multi-sess", "req-multi")
    assert transition is not None
    # With a remaining unrecorded effect the transition must stay 'planned'.
    assert transition["status"] == "planned"


def test_transition_from_rows_detects_fingerprint_mismatch() -> None:
    _persist_begin("fp-sess", "req-fp", "fp")
    altered = store._dump(_begin_command("fp-sess", "req-fp", "tampered").to_dict())
    with store.connection() as database:
        database.execute(
            "UPDATE transitions SET command_json = ? WHERE session_id = ? AND request_id = ?",
            (altered, "fp-sess", "req-fp"),
        )
        database.commit()
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        store.transition_for("fp-sess", "req-fp")


# --------------------------------------------------------------------------- #
# record_effect_result
# --------------------------------------------------------------------------- #

def test_record_effect_result_rejects_negative_ordinal() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        store.record_effect_result("s", "r", -1, {})


def test_record_effect_result_raises_when_effect_missing() -> None:
    with pytest.raises(RuntimeError, match="effect disappeared"):
        store.record_effect_result("no-sess", "no-req", 0, {})


def test_record_effect_result_returns_prior_result() -> None:
    _persist_begin("idem-sess", "req-idem", "idem")
    first = store.record_effect_result("idem-sess", "req-idem", 0, {"v": 1})
    second = store.record_effect_result("idem-sess", "req-idem", 0, {"v": 2})
    assert first == {"v": 1}
    assert second == {"v": 1}


# --------------------------------------------------------------------------- #
# commit_transition
# --------------------------------------------------------------------------- #

def test_commit_transition_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="disappeared"):
        store.commit_transition("no-sess", "no-req")


def test_commit_transition_rejects_planned_status() -> None:
    _persist_begin("planned-sess", "req-planned", "planned")
    with pytest.raises(RuntimeError, match="before effects"):
        store.commit_transition("planned-sess", "req-planned")


def test_commit_transition_is_idempotent_after_commit() -> None:
    snapshot = _finish_begin("committed-sess", "req-committed", "committed")
    again = store.commit_transition("committed-sess", "req-committed")
    assert again == snapshot


def test_commit_transition_detects_revision_change() -> None:
    _persist_begin("rev-sess", "req-rev", "rev")
    store.record_effect_result("rev-sess", "req-rev", 0, {"o": 1})
    with store.connection() as database:
        database.execute(
            "UPDATE sessions SET revision = revision + 1 WHERE session_id = ?",
            ("rev-sess",),
        )
        database.commit()
    with pytest.raises(store.SnapshotChanged, match="revision changed"):
        store.commit_transition("rev-sess", "req-rev")


def _seed_terminal_idle_transition(
    session_id: str,
    request_id: str,
    *,
    with_execution_identity: bool,
) -> None:
    _persist_begin(session_id, request_id, "terminal")
    payload: dict[str, Any] = {
        "request_id": request_id,
        "command": "finish_execution_and_turn",
        "source_phase": "running",
        "next_phase": "idle",
        "identity": _identity("terminal").to_dict(),
        "outcome": "complete",
    }
    if with_execution_identity:
        payload["execution_identity"] = ExecutionTurnIdentity(
            "execution-terminal",
            "assistant-terminal",
            "native",
        ).to_dict()
        payload["provider_run_id"] = "run-terminal"
    idle_snapshot = store._dump(LifecycleSnapshot().to_dict())
    with store.connection() as database:
        database.execute(
            "UPDATE transitions SET status = 'effects_applied', "
            "next_snapshot_json = ? WHERE session_id = ? AND request_id = ?",
            (idle_snapshot, session_id, request_id),
        )
        database.execute(
            "UPDATE effects SET payload_json = ? "
            "WHERE session_id = ? AND request_id = ? AND ordinal = 0",
            (store._dump(payload), session_id, request_id),
        )
        database.commit()


def test_commit_terminal_idle_records_pending_render() -> None:
    _seed_terminal_idle_transition(
        "term-sess", "req-term", with_execution_identity=True,
    )
    store.commit_transition("term-sess", "req-term")
    pending = store.pending_terminal_render("term-sess")
    assert pending is not None
    assert pending["outcome"] == "complete"
    assert pending["lifecycle_message_id"] == "message-terminal"
    renders = store.pending_terminal_renders()
    assert any(r["session_id"] == "term-sess" for r in renders)


def test_commit_idle_without_execution_identity_skips_render() -> None:
    _seed_terminal_idle_transition(
        "noexec-sess", "req-noexec", with_execution_identity=False,
    )
    store.commit_transition("noexec-sess", "req-noexec")
    assert store.pending_terminal_render("noexec-sess") is None


def test_pending_terminal_render_missing_returns_none() -> None:
    assert store.pending_terminal_render("never") is None


def test_acknowledge_terminal_render_is_single_shot() -> None:
    _seed_terminal_idle_transition(
        "ack-sess", "req-ack", with_execution_identity=True,
    )
    store.commit_transition("ack-sess", "req-ack")
    assert store.acknowledge_terminal_render("ack-sess", "req-ack") is True
    assert store.acknowledge_terminal_render("ack-sess", "req-ack") is False


# --------------------------------------------------------------------------- #
# mark_notification_attempted + table_counts
# --------------------------------------------------------------------------- #

def test_mark_notification_attempted_is_single_shot() -> None:
    _finish_begin("notif-sess", "req-notif", "notif")
    assert store.mark_notification_attempted("notif-sess", "req-notif") is True
    assert store.mark_notification_attempted("notif-sess", "req-notif") is False


def test_table_counts_reports_all_tables() -> None:
    counts = store.table_counts()
    assert set(counts) == {
        "sessions", "transitions", "effects", "pending_terminal_renders",
    }
    _persist_begin("count-sess", "req-count", "count")
    assert store.table_counts()["transitions"] >= 1


# --------------------------------------------------------------------------- #
# retire_session
# --------------------------------------------------------------------------- #

def test_retire_session_returns_false_on_missing_terminal_request() -> None:
    store.align_session_owner("retire-sess", "inc-retire")
    assert store.retire_session(
        "retire-sess", expected_terminal_request_id="no-such-request",
    ) is False
    # Session still present.
    with store.connection() as database:
        present = database.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", ("retire-sess",),
        ).fetchone()
    assert present is not None


def test_retire_session_returns_false_on_owner_mismatch() -> None:
    store.align_session_owner("retire-owner", "inc-retire")
    assert store.retire_session(
        "retire-owner", expected_owner_incarnation="inc-wrong",
    ) is False


def test_retire_session_allows_unbound_owner() -> None:
    _persist_begin("retire-unbound", "req-unbound", "unbound")
    # persist_plan leaves owner_incarnation NULL; allow_unbound_owner accepts it.
    assert store.retire_session(
        "retire-unbound",
        expected_owner_incarnation="anything",
        allow_unbound_owner=True,
    ) is True


def test_retire_session_with_matching_terminal_request() -> None:
    _seed_terminal_idle_transition(
        "retire-term", "req-retire-term", with_execution_identity=True,
    )
    store.commit_transition("retire-term", "req-retire-term")
    assert store.retire_session(
        "retire-term", expected_terminal_request_id="req-retire-term",
    ) is True
    assert store.pending_terminal_render("retire-term") is None


def test_retire_session_plain_delete() -> None:
    store.align_session_owner("retire-plain", "inc-plain")
    assert store.retire_session("retire-plain") is True
    assert store.session_snapshot("retire-plain").phase == "idle"


def test_retire_session_missing_returns_false() -> None:
    assert store.retire_session("never-existed") is False


# --------------------------------------------------------------------------- #
# corrupt-JSON defense (_load_object)
# --------------------------------------------------------------------------- #

def test_load_object_rejects_invalid_json() -> None:
    _finish_begin("corrupt-sess", "req-corrupt", "corrupt")
    with store.connection() as database:
        database.execute(
            "UPDATE sessions SET identity_json = '{not-json' WHERE session_id = ?",
            ("corrupt-sess",),
        )
        database.commit()
    with pytest.raises(RuntimeError, match="invalid lifecycle turn identity JSON"):
        store.session_snapshot("corrupt-sess")


def test_load_object_rejects_non_dict_json() -> None:
    _finish_begin("nd-sess", "req-nd", "nd")
    with store.connection() as database:
        database.execute(
            "UPDATE sessions SET identity_json = '[1, 2]' WHERE session_id = ?",
            ("nd-sess",),
        )
        database.commit()
    with pytest.raises(RuntimeError, match="invalid lifecycle turn identity"):
        store.session_snapshot("nd-sess")
