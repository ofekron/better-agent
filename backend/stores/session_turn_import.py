from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portable_lock
from stores.session_turn_store import SessionTurnStore
from stores.sqlite_truth_base import canonical_json, required_identifier


CUTOVER_MISMATCH_PREVIEW = 5


IMPORT_EVENT_TYPE = "turn.imported"
IMPORT_OUTBOX_TOPIC = "session.turn.imported"


class SessionTurnImportError(RuntimeError):
    pass


class CorruptSessionTree(SessionTurnImportError):
    pass


class CutoverAborted(SessionTurnImportError):
    pass


@dataclass(frozen=True)
class ImportReport:
    root_id: str
    contexts: int
    turns: int
    appended: int
    unchanged: int
    journal_cursor: int


def _extract_turn_states(root: dict) -> list[tuple[str, str, dict[str, Any]]]:
    """Every (sid, turn_id, state) message subtree in the persisted tree.

    The state is the message dict after the same volatile strip the JSON
    persistence applies, so import input equals what session.json owns —
    events stay in the observation journal, never in the aggregate.
    """
    import session_store

    tree = copy.deepcopy(root)
    session_store._strip_volatile_from_tree(tree)
    states: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()

    def collect(node: dict) -> None:
        sid = node.get("id")
        if not isinstance(sid, str) or not sid:
            raise CorruptSessionTree("session node without a string id")
        messages = node.get("messages")
        if messages is None:
            return
        if not isinstance(messages, list):
            raise CorruptSessionTree(f"messages of {sid} is not a list")
        for message in messages:
            if not isinstance(message, dict):
                raise CorruptSessionTree(f"non-dict message in {sid}")
            turn_id = message.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise CorruptSessionTree(f"message without a string id in {sid}")
            key = (sid, turn_id)
            if key in seen:
                raise CorruptSessionTree(f"duplicate message id {turn_id} in {sid}")
            seen.add(key)
            states.append((sid, turn_id, message))

    collect(tree)
    for fork in session_store._walk_forks(tree):
        collect(fork)
    return states


def import_lock_path(store: SessionTurnStore, root_id: str) -> Path:
    # Hash the root id into the file name so an id can never inject path
    # separators or exceed name limits.
    digest = hashlib.sha256(root_id.encode("utf-8")).hexdigest()[:24]
    return store.path.with_name(f"{store.path.stem}.import.{digest}.lock")


@contextmanager
def _root_import_lock(store: SessionTurnStore, root_id: str):
    """One importer per root at a time. The per-turn CAS cannot fence a stale
    tree SNAPSHOT (its version expectation is correct at apply time), so the
    snapshot read and every apply must sit inside one exclusive section."""
    with import_lock_path(store, root_id).open("a+b") as lock_file:
        portable_lock.lock_ex(lock_file.fileno())
        try:
            yield
        finally:
            portable_lock.unlock(lock_file.fileno())


def _load_root_tree(root_id: str) -> dict:
    import session_store

    root = session_store.get_root_tree(root_id)
    if root is None:
        raise SessionTurnImportError(f"root session {root_id} does not exist")
    if root.get("id") != root_id:
        raise SessionTurnImportError(
            f"{root_id} is not a root session (its root is {root.get('id')})"
        )
    return root


def import_root_turns(store: SessionTurnStore, root_id: str) -> ImportReport:
    """Idempotently import one root's complete message/turn subtree.

    Legacy stores remain authoritative: this only appends owner state to the
    turn store and records the observation-journal high-watermark linkage.
    Re-running with an unchanged tree is a no-op; a changed (or reverted)
    message appends the next aggregate version.
    """
    root_id = required_identifier("root_id", root_id)
    with _root_import_lock(store, root_id):
        return _import_root_turns_locked(store, root_id)


def _import_root_turns_locked(store: SessionTurnStore, root_id: str) -> ImportReport:
    from event_ingester import event_ingester

    # Captured before the tree read so the checkpoint never claims a
    # watermark ahead of the imported snapshot.
    _, journal_cursor, _ = event_ingester.session_event_meta(root_id)
    root = _load_root_tree(root_id)
    states = _extract_turn_states(root)
    return _apply_states(
        store,
        root_id=root_id,
        states=states,
        journal_cursor=int(journal_cursor),
    )


def _apply_states(
    store: SessionTurnStore,
    *,
    root_id: str,
    states: list[tuple[str, str, dict[str, Any]]],
    journal_cursor: int,
) -> ImportReport:
    appended = 0
    unchanged = 0
    for sid, turn_id, state in states:
        current = store.get_turn(root_id, sid, turn_id)
        if current is not None and current["state"] == state:
            unchanged += 1
            continue
        expected_version = int(current["aggregate_version"]) if current else 0
        state_hash = hashlib.sha256(
            canonical_json(state, label="session turn import state").encode("utf-8")
        ).hexdigest()
        # The target version is part of the key so re-importing a REVERTED
        # message (same content hash as an older version) still appends
        # instead of hitting the older command's idempotency record.
        result = store.apply_command(
            root_id=root_id,
            sid=sid,
            turn_id=turn_id,
            expected_version=expected_version,
            event_type=IMPORT_EVENT_TYPE,
            payload={"source": "legacy_session_json"},
            new_state=state,
            idempotency_key=f"import:v{expected_version + 1}:{state_hash}",
            outbox_topic=IMPORT_OUTBOX_TOPIC,
        )
        if result.appended:
            appended += 1
        else:
            unchanged += 1
    store.record_import_checkpoint(
        root_id=root_id, journal_cursor=int(journal_cursor), turn_count=len(states)
    )
    contexts = len({sid for sid, _, _ in states})
    return ImportReport(
        root_id=root_id,
        contexts=contexts,
        turns=len(states),
        appended=appended,
        unchanged=unchanged,
        journal_cursor=int(journal_cursor),
    )


@dataclass(frozen=True)
class CutoverReport:
    root_id: str
    import_report: ImportReport
    verified_turns: int


def cutover_root(store: SessionTurnStore, root_id: str) -> CutoverReport:
    """Flip one root's message/turn authority from legacy to sqlite.

    Preconditions the caller must guarantee: no live writer for the root
    (quiescent backend or a fenced SessionManager). Under the per-root
    import lock this re-imports, re-reads the legacy tree, and semantically
    compares; ANY mismatch aborts with the authority untouched. The flip is
    inert until SessionManager consumes the authority gate."""
    root_id = required_identifier("root_id", root_id)
    with _root_import_lock(store, root_id):
        current = store.get_owner_authority(root_id)
        if current != "legacy":
            raise CutoverAborted(f"root {root_id} owner authority is already {current}")
        import_report = _import_root_turns_locked(store, root_id)
        mismatches = verify_root_import(store, root_id)
        if mismatches:
            preview = "; ".join(mismatches[:CUTOVER_MISMATCH_PREVIEW])
            raise CutoverAborted(
                f"semantic compare failed for {root_id} "
                f"({len(mismatches)} mismatches): {preview}"
            )
        store.set_owner_authority(
            root_id, authority="sqlite", expected_authority="legacy"
        )
        return CutoverReport(
            root_id=root_id,
            import_report=import_report,
            verified_turns=import_report.turns,
        )


def revert_cutover(store: SessionTurnStore, root_id: str) -> None:
    """Roll a root back to legacy ownership. Safe while the authority gate
    has no consumers writing through sqlite; once S2 lands, reverting also
    requires exporting sqlite-side writes back into the legacy snapshot."""
    root_id = required_identifier("root_id", root_id)
    with _root_import_lock(store, root_id):
        store.set_owner_authority(
            root_id, authority="legacy", expected_authority="sqlite"
        )


def verify_root_import(store: SessionTurnStore, root_id: str) -> list[str]:
    """Semantic compare of the legacy tree against the imported aggregates.

    Empty result means every legacy turn is present with identical state and
    the store holds nothing the legacy tree lacks."""
    root_id = required_identifier("root_id", root_id)
    root = _load_root_tree(root_id)
    states = _extract_turn_states(root)
    mismatches: list[str] = []
    expected_keys = set()
    for sid, turn_id, state in states:
        expected_keys.add((sid, turn_id))
        row = store.get_turn(root_id, sid, turn_id)
        if row is None:
            mismatches.append(f"missing: {sid}/{turn_id}")
        elif row["state"] != state:
            mismatches.append(f"state drift: {sid}/{turn_id}")
    for key in store.list_turn_keys(root_id):
        if (key["sid"], key["turn_id"]) not in expected_keys:
            mismatches.append(f"extra: {key['sid']}/{key['turn_id']}")
    return mismatches
