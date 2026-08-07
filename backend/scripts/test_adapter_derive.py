"""Tests for backend.adapters.derive — pure chat-panel.md grammar, no I/O.

Run: PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_derive.py -q
"""

from __future__ import annotations

from backend.adapters.derive import (
    DerivedBody,
    build_subagent_turns,
    child_manifest,
    derive_body,
    derive_turn,
    resolve_result,
)
from backend.surface_contract.nodes import (
    AssistantTextPayload,
    ChildManifest,
    ContentStatus,
    DiagnosticCode,
    DiagnosticPayload,
    Node,
    NodeKind,
    ResultKind,
    ResultPayload,
    ToolInteractionPayload,
)

SURFACE = "surf-1"
TURN = "turn-1"


def _text(node_id, ts, seq, text, parent_id=None):
    return Node(
        cv=1, node_id=node_id, parent_id=parent_id, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.ASSISTANT_TEXT, ts=ts, seq=seq, status=ContentStatus.COMPLETE,
        payload=AssistantTextPayload(text=text),
    )


def _tool(node_id, ts, seq, name="Bash", parent_id=None):
    return Node(
        cv=1, node_id=node_id, parent_id=parent_id, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.TOOL_INTERACTION, ts=ts, seq=seq, status=ContentStatus.COMPLETE,
        payload=ToolInteractionPayload(tool_name=name, args={}, result={"output": "ok"}),
    )


def _subagent(node_id, ts, seq, renderable_child_count=1):
    return Node(
        cv=1, node_id=node_id, parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.NATIVE_SUBAGENT_TURN, ts=ts, seq=seq, status=None, payload=None,
        child_manifest=ChildManifest(renderable_child_count=renderable_child_count, has_children=True),
    )


def _lifecycle(node_id, ts, seq):
    return Node(
        cv=1, node_id=node_id, parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.LIFECYCLE_NOTICE, ts=ts, seq=seq, status=ContentStatus.COMPLETE, payload=None,
    )


def test_resolve_result_trailing_text_rule():
    items = [
        _tool("t1", 1.0, 1),
        _text("txt1", 2.0, 2, "working on it"),
        _text("txt2", 3.0, 3, "here is the answer"),
    ]
    result, consumed = resolve_result(items)
    assert [n.node_id for n in result] == ["txt1", "txt2"]
    assert [n.node_id for n in consumed] == ["txt1", "txt2"]


def test_resolve_result_no_trailing_text_falls_back_to_last_item():
    items = [_text("txt1", 1.0, 1, "intro"), _tool("t1", 2.0, 2)]
    result, consumed = resolve_result(items)
    assert [n.node_id for n in result] == ["t1"]
    assert [n.node_id for n in consumed] == ["t1"]


def test_resolve_result_empty_items():
    result, consumed = resolve_result([])
    assert result == []
    assert consumed == []


def test_resolve_result_provider_marked_final():
    marked = Node(
        cv=1, node_id="res1", parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.RESULT, ts=5.0, seq=5, status=ContentStatus.COMPLETE,
        payload=ResultPayload(result_kind=ResultKind.PROVIDER),
    )
    assoc_text = _text("txt1", 4.0, 4, "final answer", parent_id="res1")
    unrelated_text = _text("txt0", 1.0, 1, "unrelated preamble")
    items = [unrelated_text, assoc_text, marked]

    result, consumed = resolve_result(items)
    assert {n.node_id for n in result} == {"txt1", "res1"}
    assert {n.node_id for n in consumed} == {"txt1", "res1"}
    # order by (ts, seq)
    assert [n.node_id for n in result] == ["txt1", "res1"]


def test_resolve_result_provider_marked_final_with_no_associated_text():
    # No ASSISTANT_TEXT node parented to the result marker — the provider
    # branch must still return the result node itself (whose payload now
    # carries the answer text), not fall through to the trailing-text or
    # last-item heuristics.
    marked = Node(
        cv=1, node_id="res1", parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.RESULT, ts=5.0, seq=5, status=ContentStatus.COMPLETE,
        payload=ResultPayload(result_kind=ResultKind.PROVIDER, text="final answer", is_error=False),
    )
    unrelated_text = _text("txt0", 1.0, 1, "unrelated preamble")
    items = [unrelated_text, marked]

    result, consumed = resolve_result(items)
    assert [n.node_id for n in result] == ["res1"]
    assert [n.node_id for n in consumed] == ["res1"]
    assert result[0].payload.text == "final answer"


def test_derive_body_partitions_at_assistant_text_boundaries():
    items = [
        _text("txt1", 1.0, 1, "first explanation"),
        _tool("t1", 2.0, 2),
        _tool("t2", 3.0, 3),
        _text("txt2", 4.0, 4, "second explanation"),
        _tool("t3", 5.0, 5),
    ]
    body = derive_body(items, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert isinstance(body, DerivedBody)
    assert len(body.items) == 2
    assert all(n.kind == NodeKind.EXPLANATION for n in body.items)
    assert body.items[0].node_id == "explanation:txt1"
    assert body.items[0].child_manifest.renderable_child_count == 3  # txt1, t1, t2
    assert body.items[1].node_id == "explanation:txt2"
    assert body.items[1].child_manifest.renderable_child_count == 2  # txt2, t3
    assert [n.node_id for n in body.membership["explanation:txt1"]] == ["txt1", "t1", "t2"]
    assert [n.node_id for n in body.membership["explanation:txt2"]] == ["txt2", "t3"]


def test_derive_body_actions_before_any_text_form_their_own_partition():
    items = [_tool("t1", 1.0, 1), _tool("t2", 2.0, 2), _text("txt1", 3.0, 3, "then text")]
    body = derive_body(items, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert len(body.items) == 2
    assert body.items[0].node_id == "explanation:t1"
    assert body.items[0].child_manifest.renderable_child_count == 2
    assert body.items[1].node_id == "explanation:txt1"
    assert body.items[1].child_manifest.renderable_child_count == 1
    assert [n.node_id for n in body.membership["explanation:t1"]] == ["t1", "t2"]
    assert [n.node_id for n in body.membership["explanation:txt1"]] == ["txt1"]


def test_derive_body_consecutive_leading_texts_concatenate_into_one_partition():
    items = [
        _text("txt1", 1.0, 1, "part one"),
        _text("txt2", 2.0, 2, "part two"),
        _tool("t1", 3.0, 3),
    ]
    body = derive_body(items, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert len(body.items) == 1
    assert body.items[0].child_manifest.renderable_child_count == 3
    assert [n.node_id for n in body.membership[body.items[0].node_id]] == ["txt1", "txt2", "t1"]


def test_child_manifest_counts_renderable_content_items():
    manifest = child_manifest([_tool("t1", 1.0, 1)])
    assert manifest.renderable_child_count == 1
    assert manifest.has_children is True

    empty_manifest = child_manifest([])
    assert empty_manifest.renderable_child_count == 0
    assert empty_manifest.has_children is False


def test_child_manifest_excludes_in_place_notice_kinds():
    diag = Node(
        cv=1, node_id="d1", parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.DIAGNOSTIC, ts=1.0, seq=1, status=ContentStatus.COMPLETE,
        payload=DiagnosticPayload(severity="info", code=DiagnosticCode.OTHER, text="x", data=None),
    )
    manifest = child_manifest([diag, _lifecycle("lc1", 2.0, 2)])
    assert manifest.renderable_child_count == 0
    # has_children reflects structural presence, not renderability, so the
    # 3-dot gate is decided from renderable_child_count alone.
    assert manifest.has_children is True


def test_lifecycle_only_turn_has_zero_renderable_children():
    items = [_lifecycle("lc1", 1.0, 1)]
    turn = derive_turn(TURN, items, surface_id=SURFACE, cv=1)
    assert turn["prompt"] is None
    # lifecycle notice is the sole item: no trailing assistant_text and it
    # is the last item, so resolveResult's fallback picks it as the result.
    assert [n.node_id for n in turn["result"]] == ["lc1"]
    assert turn["body"].items == ()
    assert turn["body"].membership == {}
    assert turn["turn"].child_manifest.renderable_child_count == 0


def test_lifecycle_only_turn_with_no_items_at_all_has_zero_manifest():
    turn = derive_turn(TURN, [], surface_id=SURFACE, cv=1)
    assert turn["prompt"] is None
    assert turn["result"] == []
    assert turn["body"].items == ()
    assert turn["body"].membership == {}
    assert turn["turn"].child_manifest.renderable_child_count == 0
    assert turn["turn"].child_manifest.has_children is False


# ---- resolve_result: a SubAgentTurn is a BodyItem, never a turn's result ----

def test_resolve_result_skips_trailing_subagent_turn_falls_back_further():
    subagent = _subagent("subagent:tool1", 3.0, 3)
    items = [_text("txt1", 1.0, 1, "let me delegate this"), _tool("tool1", 2.0, 2), subagent]
    result, consumed = resolve_result(items)
    # No trailing ASSISTANT_TEXT, and the chronologically last item is a
    # SubAgentTurn — chat-panel.md: a BodyItem, never a turn's terminal
    # result — so the structural fallback must skip past it.
    assert [n.node_id for n in result] == ["tool1"]
    assert [n.node_id for n in consumed] == ["tool1"]


def test_resolve_result_all_preserved_in_place_yields_no_synthesized_result():
    # A turn that is JUST one subagent call: nothing left to fall back to
    # once the SubAgentTurn is excluded — no result at all (it stays a
    # plain BodyItem instead of being cannibalized into the result slot).
    result, consumed = resolve_result([_subagent("subagent:tool1", 1.0, 1)])
    assert result == []
    assert consumed == []


# ---- build_subagent_turns: sidechain segregation (chat-panel.md grammar) ----

def test_build_subagent_turns_no_sidechain_is_a_noop():
    items = [_text("txt1", 1.0, 1, "hi"), _tool("t1", 2.0, 2)]
    kept, extra = build_subagent_turns(items, {}, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert kept == items
    assert extra == {}


def test_build_subagent_turns_segregates_simple_sidechain():
    task_tool = _tool("task1", 1.0, 1, name="Task")
    side_text = _text("s1", 2.0, 2, "subagent thinking", parent_id="task1")
    side_tool = _tool("s2", 3.0, 3, parent_id="s1")
    trailing = _text("txt_final", 4.0, 4, "done")
    nodes = [task_tool, side_text, side_tool, trailing]
    is_sidechain = {"task1": False, "s1": True, "s2": True, "txt_final": False}

    kept, extra = build_subagent_turns(nodes, is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1)

    kept_ids = {n.node_id for n in kept}
    # The spawning Task tool call and the trailing text stay top-level;
    # the sidechain's own content (s1, s2) is GONE from `kept` — replaced
    # by exactly one NATIVE_SUBAGENT_TURN BodyItem, never flat-merged.
    assert kept_ids == {"task1", "subagent:task1", "txt_final"}
    subagent_node = next(n for n in kept if n.node_id == "subagent:task1")
    assert subagent_node.kind == NodeKind.NATIVE_SUBAGENT_TURN
    assert subagent_node.parent_id is None  # caller stamps it (same convention derive_body uses)
    assert subagent_node.ts == 2.0  # earliest subtree member's ts — orders correctly among siblings
    assert subagent_node.child_manifest.has_children is True

    # s1/s2 are reachable one level at a time via extra_index (children()'s
    # existing parent_id-scan mechanism) — never re-flattened into `kept`.
    assert extra["s1"].node_id == "s1" and extra["s2"].node_id == "s2"
    explanation = next(n for n in extra.values() if n.kind == NodeKind.EXPLANATION)
    assert explanation.parent_id == "subagent:task1"
    assert extra["s1"].parent_id == explanation.node_id
    assert extra["s2"].parent_id == explanation.node_id
    # Manifest parity: the subagent turn's own manifest is computed the
    # SAME way derive_turn computes a turn's ("one render contract").
    assert subagent_node.child_manifest == child_manifest([explanation])


def test_build_subagent_turns_extra_index_never_contains_the_subagent_node_itself():
    """Regression: `extra_index` used to ALSO carry the subagent turn's
    own node (unstamped, `parent_id=None`) under the same key `kept`
    already has it under, correctly stamped. Any caller that merges a
    combined index as `{**stamped_from_kept_via_caller, **extra_index}`
    (chat_adapter._build_turn_view does exactly this: `index.update(body_
    index)` — which stamps the subagent node's parent_id via `attach_
    body_items` — THEN `index.update(subagent_index)`) would have the
    correctly-stamped copy silently clobbered back to `parent_id=None`,
    corrupting `children(turn)` for any turn whose trailing body item is
    a subagent turn. `extra_index` must contain ONLY the subtree's
    descendants (reachable via the subagent node once IT is correctly
    parented by its caller) — never the anchor node itself."""
    task_tool = _tool("task1", 1.0, 1, name="Task")
    side_text = _text("s1", 2.0, 2, "subagent thinking", parent_id="task1")
    nodes = [task_tool, side_text]
    is_sidechain = {"task1": False, "s1": True}

    kept, extra = build_subagent_turns(nodes, is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1)

    assert "subagent:task1" not in extra
    assert any(n.node_id == "subagent:task1" for n in kept)


def test_build_subagent_turns_multiple_anchors_each_get_their_own_turn():
    task1 = _tool("task1", 1.0, 1, name="Task")
    s1 = _text("s1", 2.0, 2, "first subagent", parent_id="task1")
    task2 = _tool("task2", 3.0, 3, name="Task")
    s2 = _text("s2", 4.0, 4, "second subagent", parent_id="task2")
    is_sidechain = {"task1": False, "s1": True, "task2": False, "s2": True}

    kept, extra = build_subagent_turns(
        [task1, s1, task2, s2], is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1,
    )
    kept_ids = {n.node_id for n in kept}
    assert kept_ids == {"task1", "subagent:task1", "task2", "subagent:task2"}
    assert extra["s1"].parent_id != extra["s2"].parent_id  # under DIFFERENT subagent turns


def test_build_subagent_turns_nested_sidechain_recurses_one_level_at_a_time():
    # A subagent (spawned by task1) itself performs a nested Task tool
    # call (inner_task) that spawns ANOTHER sidechain — Claude Code's
    # isSidechain tag is flat (no depth field), so this must be detected
    # structurally, not by a depth counter.
    task1 = _tool("task1", 1.0, 1, name="Task")
    outer_text = _text("s1", 2.0, 2, "outer subagent working", parent_id="task1")
    inner_task = _tool("inner_task", 3.0, 3, name="Task", parent_id="s1")
    inner_text = _text("s2", 4.0, 4, "inner subagent working", parent_id="inner_task")
    is_sidechain = {"task1": False, "s1": True, "inner_task": True, "s2": True}

    kept, extra = build_subagent_turns(
        [task1, outer_text, inner_task, inner_text],
        is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1,
    )

    assert {n.node_id for n in kept} == {"task1", "subagent:task1"}
    outer_subagent = next(n for n in kept if n.node_id == "subagent:task1")
    # The OUTER subagent's own body preserves the nested subagent turn IN
    # PLACE (same _PRESERVED_IN_PLACE machinery, one level deeper) — never
    # flattens the inner sidechain into the outer subagent's Explanations.
    assert "subagent:inner_task" in extra
    inner_subagent = extra["subagent:inner_task"]
    assert inner_subagent.kind == NodeKind.NATIVE_SUBAGENT_TURN
    assert inner_subagent.parent_id == outer_subagent.node_id
    # inner_task itself (the tool call performed BY the outer subagent)
    # stays as ordinary content one level down, alongside the inner
    # subagent turn it spawned.
    assert "inner_task" in extra
    assert "s2" in extra
    assert extra["s2"].parent_id != outer_subagent.node_id  # nested one level further under inner_subagent


def test_build_subagent_turns_unresolvable_anchor_leaves_node_ungrouped():
    # A sidechain root whose parent_tool_use_id never resolved (e.g. the
    # spawning row wasn't in this batch) has parent_id=None — documented
    # gap, never guessed: it stays a plain (ungrouped) content node.
    orphan = _text("orphan", 1.0, 1, "stray sidechain content")
    is_sidechain = {"orphan": True}
    kept, extra = build_subagent_turns([orphan], is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert kept == [orphan]
    assert extra == {}


def test_derive_turn_counts_one_renderable_item_per_subagent_turn():
    # Full pipeline parity: once sidechain content is segregated (as
    # chat_adapter._build_turn_view now does before calling derive_turn),
    # the ENCLOSING turn's own manifest counts the subagent turn as
    # exactly ONE renderable BodyItem — never the (arbitrarily large)
    # count of its internal sidechain content.
    task_tool = _tool("task1", 1.0, 1, name="Task")
    side_text = _text("s1", 2.0, 2, "working", parent_id="task1")
    is_sidechain = {"task1": False, "s1": True}
    body_source, _extra = build_subagent_turns(
        [task_tool, side_text], is_sidechain, surface_id=SURFACE, turn_id=TURN, cv=1,
    )
    turn = derive_turn(TURN, body_source, surface_id=SURFACE, cv=1)
    subagent_items = [n for n in turn["body"].items if n.kind == NodeKind.NATIVE_SUBAGENT_TURN]
    assert len(subagent_items) == 1
    # turn["result"] resolved to the Task tool call itself (no trailing
    # text, subagent turn skipped per the resolve_result fix above) —
    # body.items therefore holds exactly the subagent turn, nothing else.
    assert turn["body"].items == tuple(subagent_items)
    assert turn["turn"].child_manifest.renderable_child_count == 2  # task1 (result) + subagent turn


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
