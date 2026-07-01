from __future__ import annotations


def compact_message_delta_payload(msg: dict) -> dict:
    payload = dict(msg)
    if "events" in payload:
        payload.pop("events", None)
        payload["event_payload_omitted"] = True
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
                next_worker.pop("events", None)
                omitted = True
            next_workers.append(next_worker)
        payload["workers"] = next_workers
        if omitted:
            payload["event_payload_omitted"] = True
    return payload
