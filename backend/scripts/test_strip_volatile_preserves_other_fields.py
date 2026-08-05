"""Locks `_strip_volatile_from_tree` SRP: it strips EXACTLY the
volatile fields (`isStreaming`, `events`, `workers[*].events`) plus
node-level sidecar fields (`draft_input`, `draft_input_seq`,
`draft_images`, `last_opened_at`) and leaves every other field
byte-identical across the strip -> write -> restore cycle.

If a future regression accidentally pops a non-events field (e.g. a
new `msg.workers[panel].metadata` dict), this test catches it.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import _test_home
_test_home.isolate("bc-test-strip-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402

# Fields the strip is contracted to remove from every node/message/panel.
_VOLATILE_SKIP = {
    "isStreaming",
    "events",
    "draft_input",
    "draft_input_seq",
    "draft_images",
    "last_opened_at",
}


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
        "draft_input": "some draft",
        "draft_input_seq": 999,
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
                "draft_input": "",
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


def test_strip_volatile_preserves_other_fields():
    tree = _build_rich_tree()
    original = copy.deepcopy(tree)

    popped = session_store._strip_volatile_from_tree(tree)

    # 1) After strip: every events list + isStreaming absent in the live tree.
    msg_a = tree["messages"][1]
    assert "events" not in msg_a
    assert "isStreaming" not in msg_a
    assert "events" not in msg_a["workers"][0]

    # 2) After strip: every NON-volatile field is byte-identical.
    assert _frozen_view(tree, _VOLATILE_SKIP) == _frozen_view(original, _VOLATILE_SKIP)

    # 3) After restore: tree is byte-identical to original.
    session_store._restore_volatile_to_tree(popped)
    assert json.dumps(tree, sort_keys=True) == json.dumps(original, sort_keys=True)

    # 4) Through write_session_full: tree is restored after the write.
    #    updated_at is NOT bumped, so the live tree stays EXACTLY the original.
    session_store._ensure_dir()
    session_store.write_session_full(tree, bump_updated_at=False)
    assert json.dumps(tree, sort_keys=True) == json.dumps(original, sort_keys=True)

    # 5) On-disk file omits volatile fields.
    on_disk = json.loads(session_store._session_path("root-1").read_text())
    assert "events" not in on_disk["messages"][1]
    assert "events" not in on_disk["messages"][1]["workers"][0]
    assert "events" not in on_disk["forks"][0]["messages"][0]
    assert "isStreaming" not in on_disk["messages"][1]
    assert "last_opened_at" not in on_disk
    assert "last_opened_at" not in on_disk["forks"][0]
