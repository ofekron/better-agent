"""BFF chat-tree endpoint contract.

Locks the read path: stored canonical facts in the BFF rendering cache
serve as the formal chat tree (parseProjection shape) through
adapt_chat_inputs -> project_chat -> chat_to_wire, with typed states:

  A. A cached root returns 200 with the formal tree items.
  B. A root with no cached facts returns typed 503 chat_tree_rebuilding
     (with Retry-After) and marks the root dirty on the feed client —
     never an empty-success tree.
  C. An unknown session returns 404; an invalid id returns 400.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_chat_tree.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-chat-tree-")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bff_chat_feed  # noqa: E402
import bff_chat_tree  # noqa: E402
import chat_page_cursor  # noqa: E402
import chat_projection_ingestion  # noqa: E402
from bff_runtime_service import runtime_service  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def wire_fact(seq: int, payload_type: str, payload: dict) -> dict:
    return {
        "canonical_seq": seq,
        "fact_id": f"fact-{seq}",
        "source_event_id": f"event-{seq}",
        "root_id": "root",
        "sid": "root",
        "source": "provider_stream",
        "source_stream_id": "run-1",
        "content_hash": digest(f"content-{seq}"),
        "payload_type": payload_type,
        "payload": payload,
        "observed_at": f"2026-07-15T10:00:{seq:02d}Z",
        "source_timestamp": None,
        "turn_id": "u1",
    }


SESSION = {
    "id": "root",
    "provider_id": "claude",
    "model": "sonnet-4-6",
    "reasoning_effort": "high",
    "messages": [
        {"id": "u1", "role": "user"},
        {"id": "a1", "role": "assistant",
         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
    ],
}


SESSIONS_BY_ID: dict = {
    "root": SESSION,
    "empty-root": {**SESSION, "id": "empty-root"},
    "on-demand-root": {**SESSION, "id": "on-demand-root"},
    "source-fails-root": {**SESSION, "id": "source-fails-root"},
}


async def fake_session_tree(session_id: str, *, exchange_count=None):
    from bff_runtime_service import RuntimeServiceError

    session = SESSIONS_BY_ID.get(session_id)
    if session is None:
        raise RuntimeServiceError(404, "session not found")
    return {"tree": session, "provider_kind": "claude"}


class FakeProjectionSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, root_id: str, *, after_seq: int = 0, limit: int = 500) -> dict:
        from bff_runtime_service import RuntimeServiceError

        self.calls.append((root_id, after_seq))
        if root_id == "source-fails-root":
            raise RuntimeServiceError(503, "runtime unavailable")
        if root_id != "on-demand-root":
            return {
                "found": True, "provider_kind": "claude",
                "facts": [], "next_seq": after_seq, "has_more": False,
            }
        facts = []
        for seq, payload_type, payload in [
            (1, "user_prompt", {"message_id": "u1", "text": "Run it"}),
            (2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
            (3, "assistant_output", {"message_id": "a1", "text": "Warmed.", "final": True}),
        ]:
            fact = wire_fact(seq, payload_type, payload)
            fact["root_id"] = root_id
            fact["sid"] = root_id
            facts.append(fact)
        return {
            "found": True, "provider_kind": "claude",
            "facts": facts, "next_seq": 3, "has_more": False,
        }


def main() -> None:
    for seq, payload_type, payload in [
        (1, "user_prompt", {"message_id": "u1", "text": "Run it"}),
        (2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
        (3, "assistant_output", {"message_id": "a1", "text": "All done.", "final": True}),
    ]:
        chat_projection_ingestion.admit_canonical_fact(
            wire_fact(seq, payload_type, payload), provider="claude",
        )

    original = runtime_service.session_tree
    original_feed_client = bff_chat_feed.feed_client
    runtime_service.session_tree = fake_session_tree
    projection_source = FakeProjectionSource()
    bff_chat_feed.feed_client = bff_chat_feed.ChatFeedClient(
        source_reader=projection_source,
    )
    app = FastAPI()
    app.include_router(bff_chat_tree.router)
    client = TestClient(app)
    try:
        response = client.get("/api/chat-tree/root")
        check("cached root serves the formal tree", response.status_code == 200)
        body = response.json()
        turn = next((item for item in body.get("items", []) if item.get("type") == "Turn"), None)
        check("tree contains the turn with its provider result",
              turn is not None and turn["prompt"] == "u1"
              and turn["result"] is not None and turn["result"]["text"] == "All done.")
        check("no typed drops for a clean root", body.get("dropped") == [])

        response = client.get("/api/chat-tree/on-demand-root")
        check("cold cache warms synchronously from projection source",
              response.status_code == 200)
        body = response.json()
        turn = next((item for item in body.get("items", []) if item.get("type") == "Turn"), None)
        check("synchronously warmed tree contains provider result",
              turn is not None and turn["result"] is not None
              and turn["result"]["text"] == "Warmed.")
        check("foreground warm pulls source once",
              projection_source.calls.count(("on-demand-root", 0)) == 1)

        response = client.get("/api/chat-tree/source-fails-root")
        detail = response.json().get("detail")
        check("foreground warm failure stays typed rebuilding",
              response.status_code == 503
              and isinstance(detail, dict) and detail.get("code") == "chat_tree_rebuilding"
              and response.headers.get("retry-after") == "2")
        check("foreground warm failure marks feed dirty",
              "source-fails-root" in bff_chat_feed.feed_client._dirty)

        response = client.get("/api/chat-tree/empty-root")
        detail = response.json().get("detail")
        check("uncached root is a typed rebuilding state",
              response.status_code == 503
              and isinstance(detail, dict) and detail.get("code") == "chat_tree_rebuilding"
              and response.headers.get("retry-after") == "2")
        check("uncached root marks the feed dirty",
              "empty-root" in bff_chat_feed.feed_client._dirty)

        response = client.get("/api/chat-tree/missing")
        check("unknown session is 404", response.status_code == 404)
        response = client.get("/api/chat-tree/bad.id")
        check("invalid session id is 400", response.status_code == 400)

        # Windowing: 7 turns on a fresh root; default window = last 5.
        window_session = {
            **SESSION, "id": "windowroot",
            "messages": [
                entry for turn in range(1, 8) for entry in (
                    {"id": f"u{turn}", "role": "user", "seq": turn * 10},
                    {"id": f"a{turn}", "role": "assistant", "seq": turn * 10 + 1,
                     "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                                  "reasoning_effort": "high"}},
                )
            ],
        }
        seq = 0
        for turn in range(1, 8):
            for payload_type, payload in (
                ("user_prompt", {"message_id": f"u{turn}", "text": f"prompt {turn}"}),
                ("message_ownership_declared",
                 {"message_id": f"a{turn}", "prompt_message_id": f"u{turn}"}),
                ("assistant_output",
                 {"message_id": f"a{turn}", "text": f"answer {turn}", "final": True}),
            ):
                seq += 1
                fact_payload = wire_fact(seq, payload_type, payload)
                fact_payload["root_id"] = "windowroot"
                fact_payload["sid"] = "windowroot"
                fact_payload["turn_id"] = f"u{turn}"
                chat_projection_ingestion.admit_canonical_fact(
                    fact_payload, provider="claude",
                )
        SESSIONS_BY_ID["windowroot"] = window_session

        response = client.get("/api/chat-tree/windowroot")
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("default window is the last 5 turns",
              response.status_code == 200 and turn_ids == ["u3", "u4", "u5", "u6", "u7"])
        window_page_cursor = body["page"].get("page_cursor")
        check("page carries only the signed cursor as its paging handle",
              body["page"] == {"turns": 5, "pane": "windowroot",
                               "has_older": True,
                               "page_cursor": window_page_cursor}
              and isinstance(window_page_cursor, str) and window_page_cursor)
        check("lookup carries prompt text and snapshot seq",
              body["lookup"]["u7"] == {"kind": "message", "role": "user",
                                       "text": "prompt 7", "seq": 70,
                                       "snapshot": {"id": "u7", "role": "user", "seq": 70},
                                       "historical_hydration_root": None})
        check("response carries session metadata without messages",
              body["session"]["id"] == "windowroot" and "messages" not in body["session"])
        result_part = next(item for item in body["items"]
                           if item["type"] == "Turn" and item["id"] == "u7")["result"]["part_ids"][0]
        check("lookup resolves result events to their message",
              body["lookup"][result_part]["kind"] == "event"
              and body["lookup"][result_part]["message_id"] == "a7"
              and body["lookup"][result_part]["message_seq"] == 71)

        response = client.get(f"/api/chat-tree/windowroot?cursor={window_page_cursor}")
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("older page returns the exact preceding turns with no overlap",
              turn_ids == ["u1", "u2"] and body["page"]["has_older"] is False
              and body["page"]["page_cursor"] is None)

        response = client.get("/api/chat-tree/windowroot?cursor=nope")
        detail = response.json().get("detail")
        check("stale turn cursor is a typed 409",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")

        # The legacy unbound before_turn param is gone: it is ignored, so
        # the latest window is served — no unbound load-more path remains.
        response = client.get("/api/chat-tree/windowroot?before_turn=u3")
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("bare before_turn no longer pages (unbound path removed)",
              response.status_code == 200
              and turn_ids == ["u3", "u4", "u5", "u6", "u7"])

        # Worker delegation + todos content survives from raw canonical
        # facts (worker_start, tool_call with TodoWrite args) all the way
        # through render_chat's lookup sidecar, and the runtime session
        # snapshot's `workers` panel array is no longer stripped.
        worker_todo_session = {
            **SESSION, "id": "worker-todo-root",
            "messages": [
                {"id": "wu1", "role": "user"},
                {"id": "wa1", "role": "assistant",
                 "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                              "reasoning_effort": "high"},
                 "workers": [{
                     "delegation_id": "d1", "worker_session_id": "w1",
                     "worker_description": "Do thing", "panel_kind": "worker",
                     "is_new": False, "instructions_preview": "", "events": [],
                 }]},
            ],
        }
        SESSIONS_BY_ID["worker-todo-root"] = worker_todo_session
        for seq, payload_type, payload in [
            (1, "user_prompt", {"message_id": "wu1", "text": "delegate it"}),
            (2, "message_ownership_declared", {"message_id": "wa1", "prompt_message_id": "wu1"}),
            (3, "worker_start", {
                "message_id": "wa1", "delegation_id": "d1", "worker_session_id": "w1",
                "worker_description": "Do thing", "panel_kind": "worker", "insert_at": 0,
            }),
            (4, "tool_call", {
                "message_id": "wa1", "tool_use_id": "tool-1", "tool": "TodoWrite",
                "args": {"todos": [{"content": "do X", "status": "pending"}]},
            }),
            (5, "assistant_output", {"message_id": "wa1", "text": "Delegated.", "final": True}),
        ]:
            fact = wire_fact(seq, payload_type, payload)
            fact["root_id"] = "worker-todo-root"
            fact["sid"] = "worker-todo-root"
            fact["turn_id"] = "wu1"
            chat_projection_ingestion.admit_canonical_fact(fact, provider="claude")

        response = client.get("/api/chat-tree/worker-todo-root")
        body = response.json()
        check("worker/todo root serves 200", response.status_code == 200)
        check("no typed drops for worker/todo root", body.get("dropped") == [])
        check("assistant snapshot carries the worker panel array (no longer stripped)",
              body["lookup"]["wa1"]["snapshot"]["workers"] == [{
                  "delegation_id": "d1", "worker_session_id": "w1",
                  "worker_description": "Do thing", "panel_kind": "worker",
                  "is_new": False, "instructions_preview": "", "events": [],
              }])
        worker_lookup_entry = next(
            (entry for entry in body["lookup"].values()
             if entry.get("kind") == "event" and entry.get("type") == "other_typed_work"
             and entry.get("data", {}).get("kind") == "worker_start"),
            None,
        )
        check("worker_start fact reaches the lookup sidecar with its full payload",
              worker_lookup_entry is not None
              and worker_lookup_entry["data"]["payload"]["delegation_id"] == "d1"
              and worker_lookup_entry["data"]["payload"]["worker_session_id"] == "w1")
        todo_lookup_entry = next(
            (entry for entry in body["lookup"].values()
             if entry.get("kind") == "event" and entry.get("type") == "tool_interaction"
             and entry.get("data", {}).get("tool_name") == "TodoWrite"),
            None,
        )
        check("tool_call args (todos) reach the lookup sidecar",
              todo_lookup_entry is not None
              and todo_lookup_entry["data"]["args"]["todos"]
              == [{"content": "do X", "status": "pending"}])

        # Fork-pane windowing: fork panes page through the same chat-tree
        # cursor contract as the root (chat-panel.md runtime parity /
        # load-more requirements) instead of the legacy seq paging.
        # Message seqs are per-node counters: the fork's tail reuses seq
        # numbers the root also uses — pane scoping must keep them apart.
        fork_session = {
            **SESSION, "id": "forkedroot",
            "messages": [
                {"id": "fu1", "role": "user", "seq": 1},
                {"id": "fa1", "role": "assistant", "seq": 2,
                 "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                              "reasoning_effort": "high"}},
                {"id": "fu2", "role": "user", "seq": 3},
                {"id": "fa2", "role": "assistant", "seq": 4,
                 "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                              "reasoning_effort": "high"}},
            ],
            "forks": [{
                "id": "fork1", "kind": "user", "fork_point_seq": 2,
                "messages": [
                    {"id": "fu1", "role": "user", "seq": 1},
                    {"id": "fa1", "role": "assistant", "seq": 2},
                    *(entry for turn in range(1, 4) for entry in (
                        {"id": f"ku{turn}", "role": "user", "seq": 2 + turn * 2 - 1},
                        {"id": f"ka{turn}", "role": "assistant", "seq": 2 + turn * 2,
                         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6",
                                      "reasoning_effort": "high"}},
                    )),
                ],
                "forks": [],
            }],
        }
        SESSIONS_BY_ID["forkedroot"] = fork_session
        seq = 0
        turn_facts = [
            ("forkedroot", "fu1", "fa1"), ("forkedroot", "fu2", "fa2"),
            ("fork1", "ku1", "ka1"), ("fork1", "ku2", "ka2"), ("fork1", "ku3", "ka3"),
        ]
        for sid, prompt_id, answer_id in turn_facts:
            for payload_type, payload in (
                ("user_prompt", {"message_id": prompt_id, "text": f"prompt {prompt_id}"}),
                ("message_ownership_declared",
                 {"message_id": answer_id, "prompt_message_id": prompt_id}),
                ("assistant_output",
                 {"message_id": answer_id, "text": f"answer {answer_id}", "final": True}),
            ):
                seq += 1
                fact = wire_fact(seq, payload_type, payload)
                fact["root_id"] = "forkedroot"
                fact["sid"] = sid
                fact["turn_id"] = prompt_id
                chat_projection_ingestion.admit_canonical_fact(fact, provider="claude")

        response = client.get("/api/chat-tree/forkedroot")
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("root pane excludes fork turns",
              response.status_code == 200 and turn_ids == ["fu1", "fu2"])

        response = client.get("/api/chat-tree/forkedroot?pane=fork1&turns=2")
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        fork_page_cursor = body["page"].get("page_cursor")
        check("fork pane serves only its own turns, windowed",
              response.status_code == 200 and turn_ids == ["ku2", "ku3"])
        check("fork pane page carries its pane and cursor",
              body["page"]["pane"] == "fork1"
              and body["page"]["has_older"] is True
              and isinstance(fork_page_cursor, str) and fork_page_cursor)

        response = client.get(
            f"/api/chat-tree/forkedroot?pane=fork1&turns=2&cursor={fork_page_cursor}",
        )
        body = response.json()
        turn_ids = [item["id"] for item in body["items"] if item["type"] == "Turn"]
        check("fork pane older page has no overlap and terminates",
              response.status_code == 200 and turn_ids == ["ku1"]
              and body["page"]["has_older"] is False
              and body["page"]["page_cursor"] is None)
        check("fork pane lookup resolves the fork's own prompt",
              body["lookup"]["ku1"]["kind"] == "message"
              and body["lookup"]["ku1"]["text"] == "prompt ku1")

        root_scoped_cursor = chat_page_cursor.encode_page_cursor(
            root_id="forkedroot", pane_id="forkedroot", generation=1, revision=1,
            turn_id="fu2", turn_seq=1, turn_hash="0" * 64,
        )
        response = client.get(
            f"/api/chat-tree/forkedroot?pane=fork1&cursor={root_scoped_cursor}",
        )
        detail = response.json().get("detail")
        check("root-owned cursor inside a fork pane is a typed 409",
              response.status_code == 409
              and isinstance(detail, dict) and detail.get("code") == "stale_turn_cursor")

        response = client.get("/api/chat-tree/forkedroot?pane=ghost")
        detail = response.json().get("detail")
        check("unknown pane is a typed 404",
              response.status_code == 404
              and isinstance(detail, dict) and detail.get("code") == "pane_not_found")
    finally:
        runtime_service.session_tree = original
        bff_chat_feed.feed_client = original_feed_client
        chat_projection_ingestion.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all bff chat tree tests passed")
