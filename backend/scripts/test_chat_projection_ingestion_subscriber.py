"""Regression test for the canonical chat projection admission funnel.

Pins two bugs from the original wiring, where `admit_provider_event` was
called directly from four separate producer call sites (orchs/base.py,
turn_manager.py's live path, and twice in run_recovery.py) instead of a
single subscriber:

  A. Duplication: a provider-stream event journaled through more than one
     call site must still be admitted into the canonical projection
     exactly once, not once per call site.
  B. Fragility: a failure inside canonical-projection admission (e.g. an
     unresolvable provider identity) must never propagate back into the
     journal write / apply_event render-tree path. The write must still
     succeed and be visible in events.jsonl.
  C. Filter correctness: journal facts that are not provider-stream
     content (internal bus facts) must never reach the canonical
     projection.

Run with:
    cd backend && .venv/bin/python scripts/test_chat_projection_ingestion_subscriber.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-chat-projection-subscriber-")

from event_bus import bus  # noqa: E402
from event_journal import bind_event_journal_loop, event_journal_writer, publish_event_sync  # noqa: E402
from event_bus_subscribers import bind_chat_projection_ingestion  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _seed_run(run_id: str, provider_kind: str) -> None:
    run_dir = ba_home() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "backend_state.json").write_text(
        json.dumps({"provider_kind": provider_kind}), encoding="utf-8",
    )


def _facts_for_root(root_id: str) -> list:
    import chat_projection_ingestion
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider="claude", session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    return service.read_facts(authority)


def _wait_for_fact(root_id: str, event_id: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(f.canonical_fact.get("event_id") == event_id for f in _facts_for_root(root_id)):
            return True
        time.sleep(0.02)
    return False


async def _run() -> bool:
    loop = asyncio.get_running_loop()
    bind_event_journal_loop(loop)
    event_journal_writer.register(bus)
    bind_chat_projection_ingestion()

    ok = True

    # ---- A: single admission across the two current journal producers ----
    root = "root-a"
    _seed_run("run-a", "claude")
    await asyncio.to_thread(
        publish_event_sync,
        session_id=root, event_type="agent_message",
        data={"uuid": "event-apply-path", "message": {"role": "assistant", "content": []}},
        source="apply_event", run_id="run-a", message_id="msg-a",
    )
    await asyncio.to_thread(
        publish_event_sync,
        session_id=root, event_type="agent_message",
        data={"uuid": "event-live-path", "message": {"role": "assistant", "content": []}},
        source="provider_stream", run_id="run-a", message_id="msg-a",
    )
    found_apply = _wait_for_fact(root, "event-apply-path")
    found_live = _wait_for_fact(root, "event-live-path")
    facts = _facts_for_root(root)
    ok = _check(
        found_apply and found_live and len(facts) == 2,
        "each provider-stream journal write admits exactly once",
        str([f.canonical_fact.get("event_id") for f in facts]),
    ) and ok

    # Re-publish the identical apply_event-sourced fact again (simulates a
    # second producer racing to journal the same content) and confirm no
    # duplicate lands in the canonical projection.
    await asyncio.to_thread(
        publish_event_sync,
        session_id=root, event_type="agent_message",
        data={"uuid": "event-apply-path", "message": {"role": "assistant", "content": []}},
        source="apply_event", run_id="run-a", message_id="msg-a",
        event_id="event-apply-path",
    )
    await asyncio.sleep(0.2)
    facts = _facts_for_root(root)
    ok = _check(
        len(facts) == 2,
        "re-admitting identical content does not duplicate the canonical fact",
        str(len(facts)),
    ) and ok

    # ---- B: a broken canonical-projection admission never breaks the write ----
    import chat_projection_ingestion
    original = chat_projection_ingestion.admit_provider_event

    def _boom(**_kwargs):
        raise RuntimeError("simulated canonical projection failure")

    chat_projection_ingestion.admit_provider_event = _boom
    try:
        written = await asyncio.to_thread(
            publish_event_sync,
            session_id=root, event_type="agent_message",
            data={"uuid": "event-during-outage", "message": {"role": "assistant", "content": []}},
            source="provider_stream", run_id="run-a", message_id="msg-a",
        )
    finally:
        chat_projection_ingestion.admit_provider_event = original
    ok = _check(
        written is not None and written.seq > 0,
        "journal write succeeds even when canonical projection admission raises",
        str(written),
    ) and ok
    from event_journal import event_journal_reader
    rows = event_journal_reader.read_message_events(root, "msg-a")
    ok = _check(
        any(r.get("data", {}).get("uuid") == "event-during-outage" for r in rows),
        "the durably-journaled row survives a canonical projection failure",
        str(rows),
    ) and ok

    # ---- C: non-provider journal facts are not admitted ----
    root_b = "root-b"
    await asyncio.to_thread(
        publish_event_sync,
        session_id=root_b, event_type="session.something",
        data={"uuid": "event-internal-fact"},
        source="event_bus",
    )
    await asyncio.sleep(0.2)
    ok = _check(
        not any(
            f.canonical_fact.get("event_id") == "event-internal-fact"
            for f in _facts_for_root(root_b)
        ),
        "internal (non-provider-stream) journal facts are never admitted",
    ) and ok

    import chat_projection_ingestion as cpi
    cpi.close()
    return ok


def main() -> int:
    ok = asyncio.run(_run())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
