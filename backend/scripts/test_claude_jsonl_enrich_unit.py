"""100% unit coverage of claude_jsonl_enrich.

Pure-function enrichment pipeline shared by live tailing (jsonl_tailer) and
batch replay (run_recovery): parses a claude jsonl line, walks the parentUuid
chain to resolve a sidechain's enclosing tool_use id, and maintains a
subagent registry that binds agent meta files back to their spawning
Agent/Task/Workflow tool_use. Every branch is pinned with a real assertion.

Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_claude_jsonl_enrich_unit.py
"""

from __future__ import annotations

import json
import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-jsonl-enrich-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from claude_jsonl_enrich import (  # noqa: E402
    _SubagentRegistry,
    enrich_jsonl_line,
    register_agent_tool_uses,
)


def _tu(block_id: str, name: str = "Read", **inp) -> dict:
    return {"type": "tool_use", "id": block_id, "name": name, "input": inp}


def _line(
    uuid: str | None = "u1",
    parent: str | None = None,
    content=None,
    *,
    is_sidechain: bool = False,
    **extra,
) -> str:
    msg = {"content": content if content is not None else []}
    record = {"uuid": uuid, "parentUuid": parent, "message": msg, **extra}
    if is_sidechain:
        record["isSidechain"] = True
    return json.dumps(record)


# --------------------------------------------------------------------------- #
# enrich_jsonl_line parse/reject branches
# --------------------------------------------------------------------------- #


def test_enrich_empty_line_returns_none() -> None:
    reg = _SubagentRegistry()
    assert enrich_jsonl_line("   \n", {}, {}, reg) is None
    assert enrich_jsonl_line("", {}, {}, reg) is None


def test_enrich_unparseable_returns_none() -> None:
    reg = _SubagentRegistry()
    assert enrich_jsonl_line("{not json", {}, {}, reg) is None


def test_enrich_non_dict_payload_returns_none() -> None:
    reg = _SubagentRegistry()
    assert enrich_jsonl_line("[1, 2, 3]", {}, {}, reg) is None
    assert enrich_jsonl_line("42", {}, {}, reg) is None


def test_enrich_wraps_payload_as_agent_message() -> None:
    reg = _SubagentRegistry()
    out = enrich_jsonl_line(_line(uuid="u1"), {}, {}, reg)
    assert out == {"type": "agent_message", "data": {"uuid": "u1", "parentUuid": None, "message": {"content": []}}}


def test_enrich_exception_falls_back_to_parsed() -> None:
    # message is a list -> `(list or {}).get` raises AttributeError inside
    # _enrich_claude_line -> enrich_jsonl_line falls back to the raw parsed dict.
    reg = _SubagentRegistry()
    raw = json.dumps({"uuid": "u1", "message": [1, 2, 3]})
    out = enrich_jsonl_line(raw, {}, {}, reg)
    assert out == {"type": "agent_message", "data": {"uuid": "u1", "message": [1, 2, 3]}}


def test_enrich_explicit_parent_tool_use_id_overrides() -> None:
    reg = _SubagentRegistry()
    out = enrich_jsonl_line(
        _line(uuid="u1"), {}, {}, reg, parent_tool_use_id="EXPLICIT",
    )
    assert out["data"]["parent_tool_use_id"] == "EXPLICIT"


# --------------------------------------------------------------------------- #
# _enrich_claude_line tracking + sidechain DAG walk
# --------------------------------------------------------------------------- #


def test_enrich_records_uuid_and_parent_link() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    enrich_jsonl_line(_line(uuid="child", parent="root"), tu_ids, parents, reg)
    assert parents["child"] == "root"


def test_enrich_skips_parent_link_when_missing() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    enrich_jsonl_line(_line(uuid=None, parent=None), tu_ids, parents, reg)
    assert parents == {}
    assert tu_ids == {}


def test_enrich_collects_tool_use_ids() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    content = [_tu("t1"), _tu("t2"), {"type": "text", "text": "hi"}, "not-a-dict"]
    enrich_jsonl_line(_line(uuid="u1", content=content), tu_ids, parents, reg)
    assert tu_ids["u1"] == ["t1", "t2"]


def test_enrich_skips_tool_use_block_without_id() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    content = [{"type": "tool_use", "name": "Read"}, _tu("real")]
    enrich_jsonl_line(_line(uuid="u1", content=content), tu_ids, parents, reg)
    assert tu_ids["u1"] == ["real"]


def test_enrich_ignores_non_list_content() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    # content is a string -> not a list -> no tool_use ids collected, no raise.
    out = enrich_jsonl_line(
        _line(uuid="u1", content="text-content"), tu_ids, parents, reg,
    )
    assert "u1" not in tu_ids
    assert out is not None


def test_sidechain_resolves_to_nearest_ancestor_tool_use() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    # Build a chain: grandparent(has tool_use) -> parent(no tool_use) -> child(sidechain).
    enrich_jsonl_line(
        _line(uuid="gp", content=[_tu("TU_GP")]), tu_ids, parents, reg,
    )
    enrich_jsonl_line(_line(uuid="p", parent="gp"), tu_ids, parents, reg)
    out = enrich_jsonl_line(
        _line(uuid="c", parent="p", is_sidechain=True), tu_ids, parents, reg,
    )
    # Walks c -> p (no ids) -> gp (has ids) -> uses LAST tool_use id.
    assert out["data"]["parent_tool_use_id"] == "TU_GP"


def test_sidechain_uses_last_tool_use_on_enclosing_message() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    enrich_jsonl_line(
        _line(uuid="m", content=[_tu("first"), _tu("second")]), tu_ids, parents, reg,
    )
    out = enrich_jsonl_line(
        _line(uuid="sc", parent="m", is_sidechain=True), tu_ids, parents, reg,
    )
    assert out["data"]["parent_tool_use_id"] == "second"


def test_sidechain_with_no_resolvable_ancestor_adds_no_field() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    enrich_jsonl_line(_line(uuid="p"), tu_ids, parents, reg)
    out = enrich_jsonl_line(
        _line(uuid="sc", parent="p", is_sidechain=True), tu_ids, parents, reg,
    )
    assert "parent_tool_use_id" not in out["data"]


def test_non_sidechain_does_not_resolve_parent_tool_use() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    enrich_jsonl_line(
        _line(uuid="m", content=[_tu("TU")]), tu_ids, parents, reg,
    )
    out = enrich_jsonl_line(_line(uuid="c", parent="m"), tu_ids, parents, reg)
    assert "parent_tool_use_id" not in out["data"]


def test_sidechain_cycle_does_not_loop_forever() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    # Pre-seed a cycle A <-> B with no tool_use ids anywhere.
    parents["A"] = "B"
    parents["B"] = "A"
    out = enrich_jsonl_line(
        _line(uuid="sc", parent="A", is_sidechain=True), tu_ids, parents, reg,
    )
    # Terminates without resolving; no field added.
    assert "parent_tool_use_id" not in out["data"]


def test_sidechain_without_parent_does_not_resolve() -> None:
    tu_ids: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    reg = _SubagentRegistry()
    out = enrich_jsonl_line(
        _line(uuid="sc", parent=None, is_sidechain=True), tu_ids, parents, reg,
    )
    assert "parent_tool_use_id" not in out["data"]


# --------------------------------------------------------------------------- #
# register_agent_tool_uses
# --------------------------------------------------------------------------- #


def test_register_agent_tool_uses_records_each_kind() -> None:
    reg = _SubagentRegistry()
    content = [
        _tu("a1", name="Agent", subagent_type="general-purpose", description="d1"),
        _tu("t1", name="Task", description="d2"),
        _tu("w1", name="Workflow"),
    ]
    register_agent_tool_uses({"message": {"content": content}}, reg)
    assert reg.claim("general-purpose", "d1") == "a1"
    assert reg.claim("Task", "d2") == "t1"
    assert reg.claim_workflow("wf_1") == "w1"


def test_register_falls_back_to_tool_name_when_type_absent() -> None:
    reg = _SubagentRegistry()
    # No subagent_type in input -> falls back to the tool name "Agent".
    content = [_tu("a1", name="Agent", description="only-desc")]
    register_agent_tool_uses({"message": {"content": content}}, reg)
    # Registered type is "Agent" (a tool-name) -> wildcard match on description.
    assert reg.claim("general-purpose", "only-desc") == "a1"


def test_register_skips_non_dict_message() -> None:
    reg = _SubagentRegistry()
    register_agent_tool_uses({"message": "not-a-dict"}, reg)
    assert reg.claim("Agent", "x") is None


def test_register_skips_non_list_content() -> None:
    reg = _SubagentRegistry()
    register_agent_tool_uses({"message": {"content": "x"}}, reg)
    assert reg.claim("Agent", "x") is None


def test_register_skips_non_tool_use_and_non_agent_names() -> None:
    reg = _SubagentRegistry()
    content = [
        {"type": "text", "text": "hi"},
        "not-a-dict",
        _tu("r1", name="Read"),
        _tu("ok", name="Agent", description="d"),
    ]
    register_agent_tool_uses({"message": {"content": content}}, reg)
    assert reg.claim("Agent", "d") == "ok"


def test_register_skips_non_string_id_or_type() -> None:
    reg = _SubagentRegistry()
    # id present but as int + valid str type: id is non-str -> skipped.
    block = {"type": "tool_use", "id": 5, "name": "Agent", "input": {"subagent_type": "x"}}
    register_agent_tool_uses({"message": {"content": [block]}}, reg)
    assert reg.claim("x", "") is None


def test_register_no_message_key_is_noop() -> None:
    reg = _SubagentRegistry()
    register_agent_tool_uses({}, reg)
    assert reg.claim("Agent", "x") is None


# --------------------------------------------------------------------------- #
# _SubagentRegistry register/claim/workflow
# --------------------------------------------------------------------------- #


def test_register_ignores_empty_tool_use_id_or_type() -> None:
    reg = _SubagentRegistry()
    reg.register("", "Agent", "d")
    reg.register("id1", "", "d")
    assert reg.claim("Agent", "d") is None


def test_claim_exact_match_pops_entry() -> None:
    reg = _SubagentRegistry()
    reg.register("id1", "general-purpose", "d")
    reg.register("id2", "general-purpose", "d")
    assert reg.claim("general-purpose", "d") == "id1"
    assert reg.claim("general-purpose", "d") == "id2"
    assert reg.claim("general-purpose", "d") is None


def test_claim_no_match_returns_none() -> None:
    reg = _SubagentRegistry()
    reg.register("id1", "general-purpose", "d1")
    assert reg.claim("general-purpose", "nope") is None
    assert reg.claim("other-type", "d1") is None


def test_claim_does_not_steal_across_exact_types() -> None:
    # INARIANT from the docstring: no type-only fallback. A pending entry
    # registered with a real agentType must not be claimed by a different type
    # even when description matches and no exact match exists.
    reg = _SubagentRegistry()
    reg.register("id1", "general-purpose", "d")
    assert reg.claim("researcher", "d") is None


def test_claim_workflow_binds_and_returns() -> None:
    reg = _SubagentRegistry()
    reg.register("wf1", "Workflow", "")
    assert reg.claim_workflow("wf_run_1") == "wf1"
    assert reg.get_workflow_parent("wf_run_1") == "wf1"


def test_claim_workflow_fifo_order() -> None:
    reg = _SubagentRegistry()
    reg.register("wf1", "Workflow", "")
    reg.register("wf2", "Workflow", "")
    assert reg.claim_workflow("run_a") == "wf1"
    assert reg.claim_workflow("run_b") == "wf2"


def test_claim_workflow_none_pending_returns_none() -> None:
    reg = _SubagentRegistry()
    reg.register("id1", "Agent", "d")
    assert reg.claim_workflow("run") is None


def test_get_workflow_parent_unknown_returns_none() -> None:
    reg = _SubagentRegistry()
    assert reg.get_workflow_parent("nope") is None


def test_register_uses_empty_string_for_missing_description() -> None:
    reg = _SubagentRegistry()
    reg.register("id1", "Agent", None)
    assert reg.claim("Agent", "") == "id1"
