from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import operation_execution as oe  # noqa: E402


def _ctx(request_id="rid", operation="op", deadline_at=None, receipt=None):
    return oe.OperationExecutionContext(
        request_id=request_id,
        operation=operation,
        deadline_at=deadline_at,
        record_receipt=receipt if receipt is not None else (lambda _r: None),
    )


def test_current_unbound_raises():
    with pytest.raises(RuntimeError, match="not bound"):
        oe.current()


def test_bind_exposes_context_then_unbinds():
    ctx = _ctx()
    with oe.bind(ctx):
        assert oe.current() is ctx
    with pytest.raises(RuntimeError):
        oe.current()


def test_context_carries_fields_and_receipt():
    calls = []
    ctx = _ctx(deadline_at=1.5, receipt=calls.append)
    with oe.bind(ctx):
        got = oe.current()
        assert got is ctx
        assert got.request_id == "rid"
        assert got.operation == "op"
        assert got.deadline_at == 1.5
        got.record_receipt("done")
    assert calls == ["done"]


def test_bind_nesting_resets_to_outer():
    outer = _ctx(request_id="outer")
    inner = _ctx(request_id="inner")
    with oe.bind(outer):
        assert oe.current() is outer
        with oe.bind(inner):
            assert oe.current() is inner
        assert oe.current() is outer


def test_bind_resets_token_on_exception():
    ctx = _ctx()
    with pytest.raises(ValueError, match="boom"):
        with oe.bind(ctx):
            assert oe.current() is ctx
            raise ValueError("boom")
    # finally reset fired even on exception -> context unbound again
    with pytest.raises(RuntimeError):
        oe.current()


def test_context_is_frozen():
    ctx = _ctx()
    with pytest.raises(Exception):
        ctx.request_id = "mutated"
