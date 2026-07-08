from __future__ import annotations

import hashlib
import json

# Internal bookkeeping key stamped on a live message dict by
# session_manager.apply_written_journal_event: the omitted-events
# revision, kept incrementally in sync with msg's OWN top-level events
# list at the one place that has genuine ground truth on whether a given
# mutation was a pure append or a same-slot replace (see that function for
# why this can't be done safely down here). Popped from the outgoing
# payload before it reaches the wire.
PRECOMPUTED_REVISION_KEY = "_omitted_events_revision"


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fold_revision(prev_revision: str, new_event: object) -> str:
    """Incrementally extend a revision with exactly one newly-appended
    event, without re-hashing any prior event's content."""
    return hashlib.sha256(
        f"{prev_revision}:{_hash_json(new_event)}".encode("utf-8")
    ).hexdigest()


def full_revision(events: list[object]) -> str:
    """Full, unconditionally-correct revision over an entire events list.
    Used to (re)establish a fresh baseline whenever a mutation isn't a
    provable pure append (a same-slot replace, or the first call)."""
    return _hash_json(events)


def _omitted_events_revision(events: list[object], precomputed: str = "") -> str:
    if precomputed:
        return precomputed
    return _hash_json(events)


def _events_payload_ref(msg: dict, events: list[object], precomputed: str = "") -> dict:
    ref = {"revision": _omitted_events_revision(events, precomputed)}
    message_id = msg.get("id")
    if isinstance(message_id, str) and message_id:
        ref["href"] = f"messages/{message_id}/events"
    return ref


def compact_message_delta_payload(msg: dict) -> dict:
    payload = dict(msg)
    precomputed_revision = payload.pop(PRECOMPUTED_REVISION_KEY, "")
    omitted_events: list[object] = []
    own_events_present = False
    workers_contributed = False
    if "events" in payload:
        events = payload.pop("events", None)
        if isinstance(events, list):
            omitted_events.extend(events)
            own_events_present = True
    workers = payload.get("workers")
    if isinstance(workers, list):
        next_workers = []
        for worker in workers:
            if not isinstance(worker, dict):
                next_workers.append(worker)
                continue
            next_worker = dict(worker)
            if "events" in next_worker:
                events = next_worker.pop("events", None)
                if isinstance(events, list) and events:
                    workers_contributed = True
                    omitted_events.extend(events)
            next_workers.append(next_worker)
        payload["workers"] = next_workers
    if omitted_events:
        # Only trust the precomputed revision when msg's own top-level
        # events are genuinely what's being hashed and nothing else was
        # folded in — any other shape falls back to the always-correct
        # full recompute.
        trust_precomputed = own_events_present and not workers_contributed
        payload["omitted_payloads"] = {
            "events": _events_payload_ref(
                payload,
                omitted_events,
                precomputed_revision if trust_precomputed else "",
            ),
        }
    return payload
