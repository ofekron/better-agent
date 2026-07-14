"""Locks `_strip_volatile_from_tree` SRP: it strips EXACTLY the
volatile fields (`isStreaming`, `events`, `workers[*].events`) plus
the node-level `last_opened_at` sidecar field and
leaves every other field byte-identical across the strip → write →
restore cycle.

If a future regression accidentally pops a non-events field (e.g. a
new `msg.workers[panel].metadata` dict), this test catches it.

Run with:
    cd backend && .venv/bin/python scripts/test_strip_volatile_preserves_other_fields.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-strip-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import runtime_ownership  # noqa: E402

runtime_ownership.register_current_process_writer()

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _build_rich_tree() -> dict:
    """Build a tree that exercises every shape the strip should touch
    AND many adjacent shapes it should leave alone."""
    return {
        "id": "root-1",
        "_schema_version": 9,
        "name": "rich-tree",
        "model": "sonnet",
        "cwd": "/tmp/rich",
        "orchestration_mode": "manager",
        "provider_id": "p1",
        "kind": "user",
        "parent_session_id": None,
        "inline_tags": [{"id": "t1", "label": "tagged"}],
        "adv_sync_overlays": [{"id": "o1", "state": "running"}],
        "open_file_panels": [{"id": "p1", "path": "/foo.py"}],
        "notes": [{"id": "n1", "text": "note"}],
        "pinned": True,
        "archived": False,
        "supervisor_enabled": False,
        "token_usage_total": {"input": 100, "output": 50},
        "pagination": {"total_messages": 2},
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "last_opened_at": "2026-01-01T00:00:01",
        "source": "cli",
        "messages": [
            {
                "id": "msg-1",
                "role": "user",
                "content": "hi",
                "seq": 0,
                "events": [{"type": "user_message_persisted", "data": {"x": 1}}],
                "client_id": "c1",
                "agent_message_uuid": "u-pin",
            },
            {
                "id": "msg-2",
                "role": "assistant",
                "content": "hello",
                "seq": 1,
                "isStreaming": True,
                "events": [
                    {"type": "agent_message", "data": {"uuid": "u-1", "type": "assistant"}},
                    {"type": "agent_message", "data": {"uuid": "u-2", "type": "assistant"}},
                ],
                "agent_session_id": "m-sid",
                "workers": [
                    {
                        "id": "del-1",
                        "name": "worker-a",
                        "status": "ok",
                        "events": [
                            {"type": "agent_message", "data": {"uuid": "p-1"}},
                            {"type": "agent_message", "data": {"uuid": "p-2"}},
                        ],
                    },
                ],
                "trace_id": "tr-abc",
            },
        ],
        "forks": [
            {
                "id": "fork-1",
                "_schema_version": 9,
                "parent_session_id": "root-1",
                "kind": "delegate_fork",
                "fork_closed": False,
                "name": "fork",
                "messages": [
                    {
                        "id": "fmsg-1",
                        "role": "assistant",
                        "content": "fork-content",
                        "seq": 0,
                        "isStreaming": False,
                        "events": [
                            {"type": "agent_message", "data": {"uuid": "f-1"}},
                        ],
                    },
                ],
                "next_seq": 1,
                "last_opened_at": "2026-01-01T00:00:02",
                "forks": [],
            },
        ],
    }


def _frozen_view(node: dict, skip_fields: set) -> str:
    """Return a JSON dump of node with `skip_fields` removed from
    every msg / panel / mgr dict. Lets the test assert that
    EVERYTHING ELSE is byte-identical."""
    clone = copy.deepcopy(node)
    def visit(n: dict):
        for f in skip_fields:
            n.pop(f, None)
        for m in n.get("messages") or []:
            for f in skip_fields:
                m.pop(f, None)
            for w in m.get("workers") or []:
                if isinstance(w, dict):
                    for f in skip_fields:
                        w.pop(f, None)
        for f in n.get("forks") or []:
            visit(f)
    visit(clone)
    return json.dumps(clone, sort_keys=True)


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    tree = _build_rich_tree()
    original = copy.deepcopy(tree)

    popped = session_store._strip_volatile_from_tree(tree)

    # 1) After strip: every events list is absent in the live tree.
    msg_a = tree["messages"][1]
    ok = "events" not in msg_a and "isStreaming" not in msg_a
    results.append(("strip removes msg.events + isStreaming",
                    ok, f"msg.events-in-keys={'events' in msg_a} "
                    f"isStreaming-in-keys={'isStreaming' in msg_a}"))

    ok = "events" not in msg_a["workers"][0]
    results.append(("strip removes msg.workers[0].events",
                    ok, f"keys={sorted(msg_a['workers'][0].keys())}"))

    # 2) After strip: every NON-volatile field is byte-identical.
    skip = {
        "isStreaming",
        "events",
        "last_opened_at",
    }
    ok = _frozen_view(tree, skip) == _frozen_view(original, skip)
    results.append(
        ("non-volatile fields byte-identical after strip", ok,
         "diff in non-volatile fields"))

    # 3) After restore: tree is byte-identical to original.
    session_store._restore_volatile_to_tree(popped)
    ok = json.dumps(tree, sort_keys=True) == json.dumps(original, sort_keys=True)
    results.append(
        ("restore reproduces original tree byte-identically", ok,
         "tree diverges from original after restore"))

    # 4) Through write_session_full: tree is restored after the write.
    session_store._ensure_dir()
    session_store.write_session_full(tree, bump_updated_at=False)
    # `updated_at` was NOT bumped, so the live tree should be EXACTLY
    # the original. Validate.
    ok = json.dumps(tree, sort_keys=True) == json.dumps(original, sort_keys=True)
    results.append(
        ("post-write_session_full: in-memory tree unchanged", ok,
         "live tree mutated by write_session_full"))

    # 5) On-disk file omits event-list fields.
    on_disk = json.loads(open(session_store._session_path("root-1")).read())
    ok = "events" not in on_disk["messages"][1]
    results.append(("on-disk msg.events absent after write", ok,
                    f"keys={sorted(on_disk['messages'][1].keys())}"))
    ok = "events" not in on_disk["messages"][1]["workers"][0]
    results.append(("on-disk msg.workers[0].events absent after write", ok, ""))
    ok = "events" not in on_disk["forks"][0]["messages"][0]
    results.append(("on-disk fork msg.events absent after write", ok, ""))
    # And isStreaming was stripped.
    ok = "isStreaming" not in on_disk["messages"][1]
    results.append(("on-disk msg has no isStreaming", ok, ""))
    ok = "last_opened_at" not in on_disk and "last_opened_at" not in on_disk["forks"][0]
    results.append(("on-disk nodes have no last_opened_at", ok, ""))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        runtime_ownership.unregister_current_process_writer()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
