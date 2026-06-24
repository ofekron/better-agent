"""Locks the v8 -> v9 in-place migration (existing sessions survive — no
wipe).

The manager/native consolidation changed the on-disk shape. Rather than
rejecting old records, `_v8_to_v9_migrate` flattens them on load:
  - `manager_agent_session_id` / `native_agent_session_id` collapse into
    a single `agent_session_id`, chosen by `orchestration_mode`.
  - each assistant msg's `manager` scope flattens: `manager.events` ->
    `msg.events`, `manager.session_id` -> `msg.agent_session_id`,
    `manager.workers` -> `msg.workers`; the `manager` key is dropped.
  - `supervisor_agent_session_id` is preserved untouched.
  - the migration walks the root AND every embedded fork.

Run with:
    cd backend && .venv/bin/python scripts/test_v8_to_v9_migration.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-v8v9-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _v8_record() -> dict:
    return {
        "id": "root1",
        "_schema_version": 8,
        "orchestration_mode": "manager",
        "manager_agent_session_id": "MGR-1",
        "native_agent_session_id": None,
        "supervisor_agent_session_id": "SUP-9",
        "messages": [
            {
                "id": "m1", "role": "assistant",
                "manager": {"session_id": "MGR-1",
                            "events": [{"type": "agent_message",
                                        "data": {"uuid": "e1"}}]},
                "workers": [{"delegation_id": "d1"}],
            },
            {"id": "m0", "role": "user"},
        ],
        "forks": [
            {
                "id": "f1", "_schema_version": 8,
                "orchestration_mode": "native",
                "manager_agent_session_id": None,
                "native_agent_session_id": "NAT-7",
                "messages": [
                    {"id": "fm", "role": "assistant",
                     "manager": {"session_id": "NAT-7", "events": []}},
                ],
            },
        ],
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    out = session_store._migrate_session(_v8_record())

    results.append((
        "root: single agent_session_id = mode-matched sid (MGR-1)",
        out.get("agent_session_id") == "MGR-1",
        f"got {out.get('agent_session_id')!r}",
    ))
    results.append((
        "root: old sid fields removed",
        "manager_agent_session_id" not in out
        and "native_agent_session_id" not in out,
        "old fields still present",
    ))
    results.append((
        "supervisor_agent_session_id preserved untouched",
        out.get("supervisor_agent_session_id") == "SUP-9",
        f"got {out.get('supervisor_agent_session_id')!r}",
    ))

    m1 = out["messages"][0]
    results.append((
        "msg: manager scope flattened onto msg.events",
        "manager" not in m1
        and [e["data"]["uuid"] for e in m1.get("events") or []] == ["e1"],
        f"manager_present={'manager' in m1} events={m1.get('events')}",
    ))
    results.append((
        "msg: manager.session_id moved to msg.agent_session_id",
        m1.get("agent_session_id") == "MGR-1",
        f"got {m1.get('agent_session_id')!r}",
    ))
    results.append((
        "msg: worker panels preserved at msg.workers",
        m1.get("workers") == [{"delegation_id": "d1"}],
        f"got {m1.get('workers')!r}",
    ))

    fork = out["forks"][0]
    results.append((
        "fork: native sid -> agent_session_id (NAT-7), flattened",
        fork.get("agent_session_id") == "NAT-7"
        and "native_agent_session_id" not in fork
        and "manager" not in fork["messages"][0]
        and fork["messages"][0].get("agent_session_id") == "NAT-7",
        f"fork={fork.get('agent_session_id')!r} "
        f"msg_sid={fork['messages'][0].get('agent_session_id')!r}",
    ))
    results.append((
        "schema bumped to current on root + fork",
        out.get("_schema_version") == session_store.SCHEMA_VERSION
        and fork.get("_schema_version") == session_store.SCHEMA_VERSION,
        f"root={out.get('_schema_version')} fork={fork.get('_schema_version')}",
    ))

    # Idempotent: re-migrating an already-v9 record is a no-op.
    again = session_store._migrate_session(out)
    results.append((
        "idempotent: re-migrating a v9 record changes nothing",
        again.get("agent_session_id") == "MGR-1"
        and "manager" not in again["messages"][0],
        "second migration mutated the record",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
