from __future__ import annotations

import hashlib
import json


def _omitted_events_revision(events: list[object]) -> str:
    encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _events_payload_ref(msg: dict, events: list[object]) -> dict:
    ref = {"revision": _omitted_events_revision(events)}
    message_id = msg.get("id")
    if isinstance(message_id, str) and message_id:
        ref["href"] = f"messages/{message_id}/events"
    return ref


def compact_message_delta_payload(msg: dict) -> dict:
    payload = dict(msg)
    omitted_events: list[object] = []
    if "events" in payload:
        events = payload.pop("events", None)
        if isinstance(events, list):
            omitted_events.extend(events)
    workers = payload.get("workers")
    if isinstance(workers, list):
        next_workers = []
        omitted = False
        for worker in workers:
            if not isinstance(worker, dict):
                next_workers.append(worker)
                continue
            next_worker = dict(worker)
            if "events" in next_worker:
                events = next_worker.pop("events", None)
                omitted = True
                if isinstance(events, list):
                    omitted_events.extend(events)
            next_workers.append(next_worker)
        payload["workers"] = next_workers
    if omitted_events:
        payload["omitted_payloads"] = {
            "events": _events_payload_ref(payload, omitted_events),
        }
    return payload
