from __future__ import annotations

import hashlib
import json


def _event_payload_revision(events: list[object]) -> str:
    encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compact_message_delta_payload(msg: dict) -> dict:
    payload = dict(msg)
    omitted_events: list[object] = []
    if "events" in payload:
        events = payload.pop("events", None)
        payload["event_payload_omitted"] = True
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
        if omitted:
            payload["event_payload_omitted"] = True
    if payload.get("event_payload_omitted"):
        payload["event_payload_revision"] = _event_payload_revision(omitted_events)
    return payload
