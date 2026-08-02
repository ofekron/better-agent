from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Isolate state before importing backend modules.
_TMP_HOME = tempfile.mkdtemp(prefix="tool_approval_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import tool_approval  # noqa: E402


def _make_rec(reg: tool_approval.ToolApprovalRegistry, **overrides):
    kwargs = dict(
        app_session_id="session-1",
        run_id="run-1",
        provider_kind="openai",
        tool_name="Bash",
        summary={"tool": "Bash"},
    )
    kwargs.update(overrides)
    return reg.create(**kwargs)


def _run(coro):
    return asyncio.run(coro)


def test_await_decision_false_on_timeout(monkeypatch):
    """Fail-closed: a decision that never arrives resolves False, and the
    record is cleaned up so it cannot leak or be decided afterwards."""
    monkeypatch.setattr(tool_approval, "APPROVAL_TIMEOUT_S", 0.01)
    reg = tool_approval.ToolApprovalRegistry()

    async def driver():
        rec = _make_rec(reg)
        return await reg.await_decision(rec), rec.approval_id

    result, aid = _run(driver())
    assert result is False
    assert reg.get(aid) is None  # finally popped


def test_await_decision_false_on_unexpected_future_exception():
    """An unexpected exception from the awaited future is swallowed to False
    (the HTTP handler must always return a verdict) and the record is cleaned."""
    reg = tool_approval.ToolApprovalRegistry()

    async def driver():
        rec = _make_rec(reg)
        rec.future.set_exception(RuntimeError("boom"))
        return await reg.await_decision(rec), rec.approval_id

    result, aid = _run(driver())
    assert result is False
    assert reg.get(aid) is None


def test_await_decision_returns_approved_decision():
    """A real decision unblocks await_decision with its verdict and cleans up."""
    reg = tool_approval.ToolApprovalRegistry()

    async def driver():
        rec = _make_rec(reg)
        asyncio.get_running_loop().call_later(0.0, rec.future.set_result, True)
        return await reg.await_decision(rec), rec.approval_id

    result, aid = _run(driver())
    assert result is True
    assert reg.get(aid) is None


def _make_rec_running(reg, **overrides):
    """create() needs a running loop (it binds the future to it)."""
    return _run(_create_in_loop(reg, overrides))


async def _create_in_loop(reg, overrides):
    return _make_rec(reg, **overrides)


def test_decide_false_for_unknown_approval():
    reg = tool_approval.ToolApprovalRegistry()
    assert reg.decide("does-not-exist", True) is False


def test_decide_false_for_already_decided():
    """First decision wins; a second decide on the same record is rejected."""
    reg = tool_approval.ToolApprovalRegistry()
    rec = _make_rec_running(reg)

    assert reg.decide(rec.approval_id, True) is True
    assert reg.decide(rec.approval_id, False) is False  # future already done
    assert rec.future.result() is True


def test_decide_false_for_already_rejected():
    reg = tool_approval.ToolApprovalRegistry()
    rec = _make_rec_running(reg)

    assert reg.decide(rec.approval_id, False) is True
    assert reg.decide(rec.approval_id, True) is False
    assert rec.future.result() is False


def test_decide_coerces_verdict_to_bool():
    """decide forwards the verdict through bool(), not as-is."""
    reg = tool_approval.ToolApprovalRegistry()
    rec = _make_rec_running(reg)

    assert reg.decide(rec.approval_id, 1) is True
    assert rec.future.result() is True


def test_list_for_session_filters_by_session():
    reg = tool_approval.ToolApprovalRegistry()
    a = _make_rec_running(reg, app_session_id="s-a")
    _make_rec_running(reg, app_session_id="s-b")
    c = _make_rec_running(reg, app_session_id="s-a")

    assert reg.list_for_session("s-a") == [a, c]
    assert reg.list_for_session("s-none") == []


def test_public_view_redacts_internal_fields():
    """public_view exposes only the wire-safe fields, never the Future."""
    reg = tool_approval.ToolApprovalRegistry()
    rec = _make_rec_running(reg)

    view = reg.public_view(rec)
    assert view == {
        "approval_id": rec.approval_id,
        "app_session_id": rec.app_session_id,
        "run_id": rec.run_id,
        "provider_kind": rec.provider_kind,
        "tool_name": rec.tool_name,
        "summary": rec.summary,
    }
    assert "future" not in view
