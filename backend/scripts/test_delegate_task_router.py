"""delegate_task router: target-bypass + always_new routing + detached dispatch.

Approval modes (manual / always_new_approve) need a live approval flow and are
not exercised here; this covers the non-approval routing decisions."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-dt-router-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_store
from orchestrator import Coordinator
from session_manager import manager as session_manager


def _make_coord(
    *,
    model: str = "",
    provider_id: str | None = None,
    reasoning_effort: str | None = None,
):
    sender = session_manager.create(
        name="sender",
        cwd="/repo",
        orchestration_mode="native",
        model=model or None,
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
    )
    coord = Coordinator()
    submit_calls: list[dict] = []

    def fake_submit_prompt(sid: str, params: dict) -> str:
        submit_calls.append({"sid": sid, "params": params})
        return params["_queued_id"]

    coord.submit_prompt = fake_submit_prompt  # type: ignore
    join_calls: list[dict] = []
    coord.register_mssg_turn_waiter = lambda **kw: join_calls.append(kw)  # type: ignore
    coord.turn_manager.has_active_turn = lambda sid: True  # type: ignore
    return coord, sender, join_calls, submit_calls


def test_target_bypass_dispatches_directly_no_create():
    coord, sender, join_calls, submit_calls = _make_coord()
    config_store.set_delegate_task_policy("auto")
    target = session_manager.create(name="existing", cwd="/repo", orchestration_mode="native")
    before = {s["id"] for s in session_manager.list_sessions()} if hasattr(session_manager, "list_sessions") else set()

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"], task="tangent",
        target_session_id=target["id"], model="m", cwd="/repo",
    ))
    assert res["success"] is True
    assert res["target_session_id"] == target["id"]
    assert res["created_session"] is False
    assert join_calls == [], "dispatch must be detached (no turn-join)"
    assert submit_calls[0]["sid"] == target["id"]
    assert submit_calls[0]["params"]["app_session_id"] == target["id"]


def test_always_new_creates_session_and_dispatches():
    provider_id = config_store.get_default_provider()["id"]
    coord, sender, join_calls, submit_calls = _make_coord(
        model="caller-model",
        provider_id=provider_id,
    )
    config_store.set_delegate_task_policy("always_new")

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"],
        task="brand new tangent",
        model="caller-model",
        cwd="/repo",
    ))
    assert res["success"] is True
    assert res["created_session"] is True
    assert res["created_sub_session"] is True
    assert res["target_session_id"]
    assert res["target_session_id"] != sender["id"]
    created = session_manager.get(res["target_session_id"])
    assert created is not None
    assert created["kind"] == "sub_session"
    assert created["parent_session_id"] == sender["id"]
    assert created["provider_id"] == provider_id
    assert created["model"] == "caller-model"
    assert created["reasoning_effort"] == sender["reasoning_effort"]
    assert join_calls == [], "dispatch must be detached"
    assert submit_calls[0]["sid"] == res["target_session_id"]
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]


def test_always_new_defaults_to_sender_model_and_provider():
    provider_id = config_store.get_default_provider()["id"]
    coord, sender, join_calls, submit_calls = _make_coord(
        model="sender-model",
        provider_id=provider_id,
        reasoning_effort="low",
    )
    config_store.set_delegate_task_policy("always_new")

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"],
        task="inherit caller selectors",
        cwd="/repo",
    ))

    assert res["success"] is True
    created = session_manager.get(res["target_session_id"])
    assert res["created_sub_session"] is True
    assert created["kind"] == "sub_session"
    assert created["provider_id"] == provider_id
    assert created["model"] == "sender-model"
    assert created["reasoning_effort"] == sender["reasoning_effort"]
    assert join_calls == []
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]


def test_always_new_provider_override_uses_provider_default_over_sender_model():
    active = config_store.get_default_provider()
    other_provider = config_store.add_provider({
        "name": "Other Delegate Provider",
        "kind": active.get("kind") or "claude",
        "mode": active.get("mode") or "subscription",
        "default_model": "delegate-other-model",
        "custom_models": ["delegate-other-model"],
    })
    coord, sender, join_calls, submit_calls = _make_coord(
        model="sender-model",
        provider_id=active["id"],
    )
    config_store.set_delegate_task_policy("always_new")

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"],
        task="provider default target",
        cwd="/repo",
        provider_id=other_provider["id"],
    ))

    assert res["success"] is True
    created = session_manager.get(res["target_session_id"])
    assert created["provider_id"] == other_provider["id"]
    assert created["model"] == "delegate-other-model"
    assert join_calls == []
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]


def test_always_new_provider_override_without_default_fails_closed():
    active = config_store.get_default_provider()
    no_default_provider = config_store.add_provider({
        "name": "No Default Delegate Provider",
        "kind": active.get("kind") or "claude",
        "mode": active.get("mode") or "subscription",
        "default_model": "",
        "custom_models": ["delegate-explicit-model"],
    })
    coord, sender, _join_calls, _submit_calls = _make_coord(
        model="sender-model",
        provider_id=active["id"],
    )
    config_store.set_delegate_task_policy("always_new")

    try:
        asyncio.run(coord.run_delegate_task(
            sender_session_id=sender["id"],
            task="provider no default target",
            cwd="/repo",
            provider_id=no_default_provider["id"],
        ))
    except ValueError as exc:
        assert "has no default model configured" in str(exc)
    else:
        raise AssertionError("provider override without model/default must fail closed")


def test_always_new_uses_explicit_selector_overrides():
    sender_provider_id = config_store.get_default_provider()["id"]
    override_provider_id = sender_provider_id
    coord, sender, join_calls, submit_calls = _make_coord(
        model="sender-model",
        provider_id=sender_provider_id,
        reasoning_effort="low",
    )
    config_store.set_delegate_task_policy("always_new")

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"],
        task="explicit selectors",
        cwd="/repo",
        provider_id=override_provider_id,
        model="explicit-model",
        reasoning_effort="medium",
    ))

    assert res["success"] is True
    created = session_manager.get(res["target_session_id"])
    assert res["created_sub_session"] is True
    assert created["kind"] == "sub_session"
    assert created["provider_id"] == override_provider_id
    assert created["model"] == "explicit-model"
    assert created["reasoning_effort"] == "medium"
    assert join_calls == []
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]


def test_always_new_can_create_standalone_session_per_call():
    provider_id = config_store.get_default_provider()["id"]
    coord, sender, join_calls, submit_calls = _make_coord(
        model="sender-model",
        provider_id=provider_id,
        reasoning_effort="low",
    )
    config_store.set_delegate_task_policy("always_new")

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"],
        task="standalone target",
        cwd="/repo",
        sub_session=False,
    ))

    assert res["success"] is True
    assert res["created_session"] is True
    assert res["created_sub_session"] is False
    created = session_manager.get(res["target_session_id"])
    assert created["kind"] == "user"
    assert created["parent_session_id"] is None
    assert created["provider_id"] == provider_id
    assert join_calls == []
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]


def test_auto_with_no_search_result_creates_session(monkeypatch=None):
    import session_search
    provider_id = config_store.get_default_provider()["id"]
    coord, sender, join_calls, submit_calls = _make_coord(
        model="sender-model",
        provider_id=provider_id,
        reasoning_effort="low",
    )
    config_store.set_delegate_task_policy("auto")
    session_search.search = lambda *a, **kw: asyncio.sleep(0, result={"session_ids": []})  # type: ignore

    res = asyncio.run(coord.run_delegate_task(
        sender_session_id=sender["id"], task="nothing fits", model="m", cwd="/repo",
    ))
    assert res["success"] is True
    assert res["created_session"] is True  # no suggestion → created
    assert res["created_sub_session"] is True
    created = session_manager.get(res["target_session_id"])
    assert created["kind"] == "sub_session"
    assert created["provider_id"] == provider_id
    assert created["reasoning_effort"] == sender["reasoning_effort"]
    assert join_calls == []
    assert submit_calls[0]["sid"] == res["target_session_id"]
    assert submit_calls[0]["params"]["app_session_id"] == res["target_session_id"]
