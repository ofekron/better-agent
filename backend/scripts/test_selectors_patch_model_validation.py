from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-selectors-model-validation-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import config_store  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
import main  # noqa: E402
from render_tree_hydrate import hydrate_msg_events_from_jsonl  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _patch(client: TestClient, sid: str, body: dict):
    return client.patch(f"/api/sessions/{sid}/selectors", json=body)


def main_test() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})

    # Two providers with disjoint, deterministic model sets (custom_models
    # are always included in available_models, no catalog fetch needed).
    prov_a = config_store.add_provider({
        "name": "Provider A",
        "kind": "claude",
        "mode": "subscription",
        "default_model": "model-a",
        "custom_models": ["model-a"],
    })
    prov_b = config_store.add_provider({
        "name": "Provider B",
        "kind": "claude",
        "mode": "subscription",
        "default_model": "model-b",
        "custom_models": ["model-b"],
    })
    a_id, b_id = prov_a["id"], prov_b["id"]
    config_store.set_default_provider(a_id)

    session = session_manager.create(
        name="victim",
        cwd="/repo",
        orchestration_mode="native",
        model="model-a",
        provider_id=a_id,
    )
    sid = session["id"]
    session_manager.append_assistant_msg(sid, {
        "id": "assistant-model-switch",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    })

    # 1) Cross-provider model alone -> 400, on-disk record UNCHANGED.
    #    This is the exact corruption: provider B's model PATCHed onto a
    #    provider-A session (the glm-5.2-onto-Claude bug).
    r = _patch(client, sid, {"model": "model-b"})
    assert r.status_code == 400, r.text
    assert "does not support model" in r.text, r.text
    rec = session_manager.get(sid) or {}
    assert rec["model"] == "model-a", rec.get("model")
    assert rec["provider_id"] == a_id, rec.get("provider_id")

    # 2) provider_id + model together (documented mutable switch) -> 200.
    #    Model is validated against the BODY's provider_id, so the legit
    #    Claude->Z.AI+glm-5.2 style switch must NOT be rejected.
    r = _patch(client, sid, {"provider_id": b_id, "model": "model-b"})
    assert r.status_code == 200, r.text
    rec = session_manager.get(sid) or {}
    assert rec["model"] == "model-b", rec.get("model")
    assert rec["provider_id"] == b_id, rec.get("provider_id")
    rows = event_ingester.read_ws_events(sid, sid_filter=sid, msg_id_filter="assistant-model-switch")
    journal_switches = [e for e in rows if e.get("type") == "model_switched"]
    assert len(journal_switches) == 1, rows
    assert journal_switches[0]["data"]["previous_model"] == "model-a"
    assert journal_switches[0]["data"]["model"] == "model-b"

    # 3) Same-provider model write still works.
    r = _patch(client, sid, {"model": "model-b"})
    assert r.status_code == 200, r.text
    rows = event_ingester.read_ws_events(sid, sid_filter=sid, msg_id_filter="assistant-model-switch")
    assert len([e for e in rows if e.get("type") == "model_switched"]) == 1

    r = _patch(client, sid, {"reasoning_effort": "high"})
    assert r.status_code == 200, r.text
    rows = event_ingester.read_ws_events(sid, sid_filter=sid, msg_id_filter="assistant-model-switch")
    effort_switches = [e for e in rows if e.get("type") == "model_switched"]
    assert len(effort_switches) == 2
    assert "reasoning_effort" in effort_switches[-1]["data"]["changed"]
    assert effort_switches[-1]["data"]["reasoning_effort"] == "high"
    streaming_rec = session_manager.get(sid) or {}
    streaming_anchors = [
        m for m in streaming_rec.get("messages", [])
        if m.get("role") == "assistant" and m.get("source") == "selector_change"
    ]
    assert streaming_anchors == [], streaming_rec.get("messages")

    finalized = session_manager.create(
        name="finalized-switch",
        cwd="/repo",
        orchestration_mode="native",
        model="model-a",
        provider_id=a_id,
    )
    finalized_sid = finalized["id"]
    session_manager.append_assistant_msg(finalized_sid, {
        "id": "assistant-completed-before-switch",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": False,
    })
    r = _patch(client, finalized_sid, {"provider_id": b_id, "model": "model-b"})
    assert r.status_code == 200, r.text
    finalized_rec = session_manager.get(finalized_sid) or {}
    finalized_messages = finalized_rec.get("messages", [])
    assert len(finalized_messages) == 2, finalized_messages
    assert finalized_messages[0].get("id") == "assistant-completed-before-switch"
    finalized_anchor = finalized_messages[-1]
    assert finalized_anchor.get("id") != "assistant-completed-before-switch"
    assert finalized_anchor.get("source") == "selector_change", finalized_messages
    anchor_events = [
        e for e in finalized_anchor.get("events", [])
        if e.get("type") == "model_switched"
    ]
    assert len(anchor_events) == 1, finalized_anchor.get("events")
    assert anchor_events[0]["data"]["previous_model"] == "model-a"
    assert anchor_events[0]["data"]["model"] == "model-b"
    old_rows = event_ingester.read_ws_events(
        finalized_sid,
        sid_filter=finalized_sid,
        msg_id_filter="assistant-completed-before-switch",
    )
    assert not [e for e in old_rows if e.get("type") == "model_switched"], old_rows
    cold_tree = copy.deepcopy(finalized_rec)
    for message in cold_tree.get("messages", []):
        message["events"] = []
    hydrate_msg_events_from_jsonl(cold_tree)
    cold_anchor_events = [
        e for e in (cold_tree.get("messages", [])[-1].get("events", []))
        if e.get("type") == "model_switched"
    ]
    assert len(cold_anchor_events) == 1, cold_tree.get("messages", [])[-1]

    # 3b) A selector switch before any assistant message exists still needs
    #     a durable event anchor so the UI can show the switch in the session.
    empty = session_manager.create(
        name="empty-switch",
        cwd="/repo",
        orchestration_mode="native",
        model="model-a",
        provider_id=a_id,
    )
    empty_sid = empty["id"]
    r = _patch(client, empty_sid, {"provider_id": b_id, "model": "model-b"})
    assert r.status_code == 200, r.text
    empty_rec = session_manager.get(empty_sid) or {}
    anchors = [
        m for m in empty_rec.get("messages", [])
        if m.get("role") == "assistant" and m.get("source") == "selector_change"
    ]
    assert len(anchors) == 1, empty_rec.get("messages")
    rows = event_ingester.read_ws_events(
        empty_sid,
        sid_filter=empty_sid,
        msg_id_filter=anchors[0]["id"],
    )
    empty_switches = [e for e in rows if e.get("type") == "model_switched"]
    assert len(empty_switches) == 1, rows
    assert empty_switches[0]["data"]["previous_provider_name"] == "Provider A"
    assert empty_switches[0]["data"]["provider_name"] == "Provider B"

    # 4) Fail-closed: when the session record yields no provider_id (and the
    #    body carries none), validation must NOT fall through to the default
    #    provider (the fail-open hole). The model write is rejected.
    async def _fake_lite(_sid):
        return {}
    orig_lite = main._session_lite
    main._session_lite = _fake_lite
    try:
        r = _patch(client, sid, {"model": "model-a"})
    finally:
        main._session_lite = orig_lite
    assert r.status_code == 400, r.text

    # 5) include_retired wiring: the selectors path validates against
    #    available_models_INCLUDING_RETIRED, so a within-sticky retired model
    #    is accepted where plain available_models would 400.
    import models as models_mod
    orig_active = models_mod.available_models
    orig_incl = models_mod.available_models_including_retired
    models_mod.available_models = lambda _pid=None: []
    models_mod.available_models_including_retired = lambda _pid=None: ["model-retired"]
    try:
        r = _patch(client, sid, {"provider_id": b_id, "model": "model-retired"})
    finally:
        models_mod.available_models = orig_active
        models_mod.available_models_including_retired = orig_incl
    assert r.status_code == 200, r.text

    print("test_selectors_patch_model_validation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
