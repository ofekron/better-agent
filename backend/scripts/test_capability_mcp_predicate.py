"""Capability scoping (Build #1): an MCP entrypoint can be gated on the
session's active-capability set via a declarative `contains` predicate clause.

Before this change the MCP predicate grammar had only equals/not_equals/nonempty,
so there was no way to say "include this MCP only when capability X is active for
this session". `_validate_mcp_predicate({"contains": ...})` raised on an unknown
key, so the membership assertions below failed. After the change, a capability's
MCP server rides the existing extension-MCP delivery path, gated by
`contains: {active_capability_ids: <cap-id>}` against the per-session active set
threaded off the session record into the run inputs.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="cap_pred_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from extension_store import _validate_mcp_predicate, _mcp_predicate_matches  # noqa: E402


def test_contains_gates_on_active_capability_membership():
    pred = _validate_mcp_predicate({"contains": {"active_capability_ids": "ofek.testape"}})
    assert _mcp_predicate_matches(pred, {"active_capability_ids": ["ofek.testape", "other"]})
    assert not _mcp_predicate_matches(pred, {"active_capability_ids": ["other"]})
    assert not _mcp_predicate_matches(pred, {"active_capability_ids": []})
    # Missing/non-list input fails closed — a scoped MCP never leaks in.
    assert not _mcp_predicate_matches(pred, {})
    assert not _mcp_predicate_matches(pred, {"active_capability_ids": "ofek.testape"})


def test_contains_validates_shape_and_unknown_keys():
    for bad in ({"contains": []}, {"contains": "x"}, {"contains": {1: "x"}}):
        try:
            _validate_mcp_predicate(bad)
        except Exception:
            pass
        else:
            raise AssertionError(f"expected rejection for {bad!r}")
    try:
        _validate_mcp_predicate({"bogus": {}})
    except Exception:
        pass
    else:
        raise AssertionError("expected rejection of unknown predicate key")


def test_existing_clauses_unaffected():
    pred = _validate_mcp_predicate(
        {"equals": {"mode": "native"}, "not_equals": {"working_mode": "search_worker"},
         "nonempty": ["app_session_id"]}
    )
    assert _mcp_predicate_matches(
        pred, {"mode": "native", "working_mode": "", "app_session_id": "s1"}
    )
    assert not _mcp_predicate_matches(
        pred, {"mode": "manager", "working_mode": "", "app_session_id": "s1"}
    )
    assert not _mcp_predicate_matches(
        pred, {"mode": "native", "working_mode": "search_worker", "app_session_id": "s1"}
    )
    assert not _mcp_predicate_matches(
        pred, {"mode": "native", "working_mode": "", "app_session_id": ""}
    )


def test_contains_composes_with_other_clauses():
    pred = _validate_mcp_predicate(
        {"equals": {"mode": "native"}, "contains": {"active_capability_ids": "ofek.testape"}}
    )
    assert _mcp_predicate_matches(
        pred, {"mode": "native", "active_capability_ids": ["ofek.testape"]}
    )
    assert not _mcp_predicate_matches(
        pred, {"mode": "native", "active_capability_ids": []}
    )
    assert not _mcp_predicate_matches(
        pred, {"mode": "manager", "active_capability_ids": ["ofek.testape"]}
    )


if __name__ == "__main__":
    import shutil

    try:
        test_contains_gates_on_active_capability_membership()
        test_contains_validates_shape_and_unknown_keys()
        test_existing_clauses_unaffected()
        test_contains_composes_with_other_clauses()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
