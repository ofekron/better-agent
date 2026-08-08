"""Tests for backend.adapters.derive — pure chat-panel.md grammar, no I/O.

Run: PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_derive.py -q
"""

from __future__ import annotations

from backend.adapters.derive import (
    DerivedBody,
    attach_compaction_manifests,
    build_subagent_panel_container,
    build_subagent_turns,
    build_subagent_turns_for_panels,
    child_manifest,
    compaction_child_manifest,
    delegation_id_of,
    derive_body,
    derive_turn,
    extract_compaction_children,
    resolve_result,
    worker_turn_container_id,
)
from backend.surface_contract.nodes import (
    AssistantTextPayload,
    ChildManifest,
    CompactionOrigin,
    CompactionPayload,
    ContentStatus,
    DiagnosticCode,
    DiagnosticPayload,
    Node,
    NodeKind,
    PromptOrigin,
    ResultKind,
    ResultPayload,
    SubAgentTurnPayload,
    ToolInteractionPayload,
    TypedPromptPayload,
    WorkerInteractionPayload,
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


def _worker_fact(node_id, ts, seq, fact_kind, delegation_id, **extra_fact):
    fact = {"delegation_id": delegation_id, **extra_fact}
    return Node(
        cv=1, node_id=node_id, parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.WORKER_INTERACTION, ts=ts, seq=seq, status=ContentStatus.COMPLETE,
        payload=WorkerInteractionPayload(fact_kind=fact_kind, fact=fact),
    )


def _compaction(node_id, ts, seq):
    return Node(
        cv=1, node_id=node_id, parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.COMPACTION, ts=ts, seq=seq, status=ContentStatus.COMPLETE,
        payload=CompactionPayload(origin=CompactionOrigin.NATIVE, summary=""),
    )


def _replay_prompt(node_id, ts, seq, parent_id, text="replayed prompt"):
    return Node(
        cv=1, node_id=node_id, parent_id=parent_id, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.TYPED_PROMPT, ts=ts, seq=seq, status=ContentStatus.COMPLETE,
        payload=TypedPromptPayload(text=text, origin=PromptOrigin.USER),
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


def test_derive_body_explanation_carries_time_range_of_its_members():
    """Explanation/group container chip data: `child_manifest.started_ts`/
    `ended_ts` are the earliest/latest `ts` among the partition's own
    members (legacy `AutoActionGroup`'s count+lead-time chip's backend-
    sourced, richer counterpart — see ADR's explanation-chip ruling)."""
    items = [
        _text("txt1", 1.0, 1, "first explanation"),
        _tool("t1", 2.0, 2),
        _tool("t2", 5.0, 3),
        _text("txt2", 6.0, 4, "second explanation"),
        _tool("t3", 9.0, 5),
    ]
    body = derive_body(items, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert body.items[0].child_manifest.started_ts == 1.0
    assert body.items[0].child_manifest.ended_ts == 5.0
    assert body.items[1].child_manifest.started_ts == 6.0
    assert body.items[1].child_manifest.ended_ts == 9.0


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


def test_child_manifest_time_range_uses_min_max_not_list_order():
    """`child_manifest()` (the shared helper `derive_turn`/`build_subagent_
    turns` use, unlike `derive_body`'s own inline construction) must derive
    started_ts/ended_ts from min/max `ts`, not first/last LIST position —
    its callers' node lists aren't guaranteed pre-sorted (e.g.
    `derive_turn`'s body+result concatenation)."""
    manifest = child_manifest([_tool("t2", 9.0, 2), _tool("t1", 3.0, 1)])
    assert manifest.started_ts == 3.0
    assert manifest.ended_ts == 9.0

    empty_manifest = child_manifest([])
    assert empty_manifest.started_ts is None
    assert empty_manifest.ended_ts is None


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


def test_derive_turn_stamps_orchestration_mode_onto_turn_node_only():
    """Team/native turn chip source: `derive_turn`'s `orchestration_mode`
    kwarg is stamped verbatim onto the TURN node (never guessed/derived —
    the caller, `chat_adapter.py`, resolves it from the owning session's
    frozen `orchestration_mode` record field). Defaults to None when the
    caller has none to offer (e.g. an unresolvable session record)."""
    items = [_text("txt1", 1.0, 1, "hi")]
    turn = derive_turn(TURN, items, surface_id=SURFACE, cv=1, orchestration_mode="team")
    assert turn["turn"].orchestration_mode == "team"

    turn_default = derive_turn(TURN, items, surface_id=SURFACE, cv=1)
    assert turn_default["turn"].orchestration_mode is None


# ---- compaction-children segregation (Item 3) ----

def test_extract_compaction_children_no_compaction_is_a_noop():
    items = [_text("txt1", 1.0, 1, "hi"), _tool("t1", 2.0, 2)]
    kept, by_id = extract_compaction_children(items)
    assert kept == items
    assert by_id == {}


def test_extract_compaction_children_pulls_children_out_leaves_compaction_node():
    compaction = _compaction("c1", 1.0, 1)
    child1 = _replay_prompt("c1:replayed:0", 0.5, 0, parent_id="c1", text="original ask")
    child2 = _text("c1:replayed:1", 0.6, 1, "reply", parent_id="c1")
    other = _text("txt2", 2.0, 2, "after compaction")
    kept, by_id = extract_compaction_children([compaction, child1, child2, other])
    assert [n.node_id for n in kept] == ["c1", "txt2"]
    assert set(by_id.keys()) == {"c1"}
    assert [n.node_id for n in by_id["c1"]] == ["c1:replayed:0", "c1:replayed:1"]  # ts order


def test_extract_compaction_children_multiple_compactions_each_own_bucket():
    c1 = _compaction("c1", 1.0, 1)
    c2 = _compaction("c2", 3.0, 3)
    kept, by_id = extract_compaction_children([
        c1, _text("c1:replayed:0", 0.5, 0, "a", parent_id="c1"),
        c2, _text("c2:replayed:0", 2.5, 2, "b", parent_id="c2"),
    ])
    assert [n.node_id for n in kept] == ["c1", "c2"]
    assert set(by_id.keys()) == {"c1", "c2"}


def test_compaction_child_manifest_counts_every_child_including_typed_prompt():
    # Unlike `child_manifest()`, TYPED_PROMPT is NOT excluded here — every
    # compaction-replay child is real transcript content, not a turn's own
    # anchoring prompt.
    manifest = compaction_child_manifest((
        _replay_prompt("c1:replayed:0", 0.5, 0, parent_id="c1"),
        _text("c1:replayed:1", 0.6, 1, "reply", parent_id="c1"),
    ))
    assert manifest.renderable_child_count == 2
    assert manifest.has_children is True


def test_compaction_child_manifest_empty():
    manifest = compaction_child_manifest(())
    assert manifest.renderable_child_count == 0
    assert manifest.has_children is False


def test_attach_compaction_manifests_fills_in_manifest_for_flat_delivery():
    compaction = _compaction("c1", 1.0, 1)
    child = _replay_prompt("c1:replayed:0", 0.5, 0, parent_id="c1")
    other = _text("txt2", 2.0, 2, "unrelated")
    out = attach_compaction_manifests([compaction, child, other])
    by_id = {n.node_id: n for n in out}
    assert by_id["c1"].child_manifest.renderable_child_count == 1
    assert by_id["c1"].child_manifest.has_children is True
    # Children/unrelated nodes pass through unchanged, still present (flat
    # delivery — no extraction on this path).
    assert by_id["c1:replayed:0"] is child
    assert by_id["txt2"] is other


def test_attach_compaction_manifests_no_children_present_leaves_manifest_none():
    # No replacement_history on this row at all (the common real-world
    # case today) — matches every other non-container node's default
    # `child_manifest=None`, never a forced `ChildManifest(0, False)`.
    compaction = _compaction("c1", 1.0, 1)
    out = attach_compaction_manifests([compaction])
    assert out[0].child_manifest is None


def test_attach_compaction_manifests_no_compaction_nodes_is_a_noop():
    items = [_text("txt1", 1.0, 1, "hi")]
    out = attach_compaction_manifests(items)
    assert out == items


# ---- build_subagent_turns_for_panels / build_subagent_panel_container ----
# (SubAgentTurn family: worker/sub_session/session panel grouping)

def test_build_subagent_turns_for_panels_no_worker_facts_is_a_noop():
    items = [_text("txt1", 1.0, 1, "hi"), _tool("t1", 2.0, 2)]
    kept, extra = build_subagent_turns_for_panels(items, surface_id=SURFACE, turn_id=TURN, cv=1)
    assert kept == items
    assert extra == {}


def test_build_subagent_turns_for_panels_groups_one_delegation():
    start = _worker_fact(
        "worker:1", 1.0, 1, "worker_start", "deleg-1",
        panel_kind="worker", worker_description="fix the bug", worker_session_id="sess-x",
    )
    event = _worker_fact("worker:2", 2.0, 2, "worker_event", "deleg-1")
    complete = _worker_fact(
        "worker:3", 3.0, 3, "worker_complete", "deleg-1",
        token_usage={"input_tokens": 10, "output_tokens": 5},
    )
    trailing = _text("txt_final", 4.0, 4, "done")
    nodes = [start, event, complete, trailing]

    kept, extra = build_subagent_turns_for_panels(nodes, surface_id=SURFACE, turn_id=TURN, cv=1)

    kept_ids = {n.node_id for n in kept}
    container_id = worker_turn_container_id("deleg-1")
    assert kept_ids == {container_id, "txt_final"}
    container = next(n for n in kept if n.node_id == container_id)
    assert container.kind == NodeKind.WORKER_TURN
    assert container.parent_id is None  # caller stamps it (same convention as build_subagent_turns)
    assert container.ts == 1.0  # earliest fact's ts
    assert container.payload == SubAgentTurnPayload(label="fix the bug")
    assert container.target_ref.session_id == "sess-x"
    # This fixture's `worker_complete` carries no `target_turn_id` — honest
    # None (see test_build_subagent_panel_container_target_turn_id_from_
    # worker_complete for the positive case).
    assert container.target_ref.turn_id is None
    assert container.sidecar_ref == "sess-x"
    assert container.child_manifest == ChildManifest(
        renderable_child_count=3, has_children=True, started_ts=1.0, ended_ts=3.0,
    )
    assert container.usage.input_tokens == 10
    assert container.usage.output_tokens == 5
    assert container.usage.total_tokens == 15

    # Every grouped fact is reachable one level down via extra_index, never
    # re-flattened into `kept` — same contract as build_subagent_turns.
    assert extra["worker:1"].parent_id == container_id
    assert extra["worker:2"].parent_id == container_id
    assert extra["worker:3"].parent_id == container_id


def test_build_subagent_turns_for_panels_panel_kind_maps_to_node_kind():
    cases = [
        ("worker", NodeKind.WORKER_TURN),
        ("sub_session", NodeKind.SUB_SESSION_TURN),
        ("sub_session_created", NodeKind.SUB_SESSION_TURN),
        ("session", NodeKind.SESSION_TURN),
        ("session_created", NodeKind.SESSION_TURN),
    ]
    for panel_kind, expected_kind in cases:
        start = _worker_fact("w:1", 1.0, 1, "worker_start", "d1", panel_kind=panel_kind)
        kept, _extra = build_subagent_turns_for_panels([start], surface_id=SURFACE, turn_id=TURN, cv=1)
        container = kept[0]
        assert container.kind == expected_kind, f"panel_kind={panel_kind}"


def test_build_subagent_turns_for_panels_multiple_delegations_each_get_their_own_turn():
    d1 = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    d2 = _worker_fact("w:2", 2.0, 2, "worker_start", "deleg-2", panel_kind="sub_session")

    kept, extra = build_subagent_turns_for_panels([d1, d2], surface_id=SURFACE, turn_id=TURN, cv=1)

    container_ids = {n.node_id for n in kept}
    assert container_ids == {worker_turn_container_id("deleg-1"), worker_turn_container_id("deleg-2")}
    assert extra["w:1"].parent_id == worker_turn_container_id("deleg-1")
    assert extra["w:2"].parent_id == worker_turn_container_id("deleg-2")


def test_build_subagent_turns_for_panels_without_worker_complete_has_no_usage():
    start = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    kept, _extra = build_subagent_turns_for_panels([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert kept[0].usage is None


def test_build_subagent_turns_for_panels_without_worker_session_id_has_no_target_ref():
    start = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    kept, _extra = build_subagent_turns_for_panels([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert kept[0].target_ref is None


def test_build_subagent_panel_container_resolves_fields_from_any_fact_in_the_group():
    # A group missing its own worker_start (live path, mid-stream) still
    # resolves panel_kind/worker_session_id/label from whichever fact
    # carries them — every producer re-sends these on every fact.
    event = _worker_fact(
        "w:2", 2.0, 2, "worker_event", "deleg-1",
        panel_kind="sub_session", worker_description="delegated task", worker_session_id="sess-y",
    )
    container = build_subagent_panel_container([event], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.kind == NodeKind.SUB_SESSION_TURN
    assert container.payload.label == "delegated task"
    assert container.target_ref.session_id == "sess-y"


def test_build_subagent_panel_container_defaults_to_worker_turn_when_panel_kind_unknown():
    # No fact in the group carries panel_kind (a pathological/ordering
    # edge case — every real producer always sends it) — falls back to
    # WORKER_TURN rather than crashing or leaving kind unset.
    event = _worker_fact("w:2", 2.0, 2, "worker_event", "deleg-1")
    container = build_subagent_panel_container([event], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.kind == NodeKind.WORKER_TURN


def test_build_subagent_panel_container_created_true_from_worker_start_is_new():
    # `emit_session_created_panel`'s "*_created" panels stamp `is_new:
    # True` on their (only) `worker_start` fact — the render-time signal
    # for legacy's distinct "created" panel variant, since no dedicated
    # NodeKind exists for it (see SubAgentTurnPayload.created's docstring).
    start = _worker_fact(
        "w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="sub_session_created",
        worker_description="a sub-session created", worker_session_id="sess-c", is_new=True,
    )
    container = build_subagent_panel_container([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.payload.created is True


def test_build_subagent_panel_container_created_false_when_is_new_absent():
    # Every non-"*_created" producer either omits `is_new` or sends False
    # — either way `created` stays False, never guessed True.
    start = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    container = build_subagent_panel_container([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.payload.created is False


def test_build_subagent_panel_container_target_turn_id_from_worker_complete():
    # `orchestrator._team_message_target_turn_id` threads the target
    # session's own TYPED_PROMPT node_id onto the `worker_complete` fact
    # once the delegation completes — the ONLY fact kind that ever carries
    # it (see build_subagent_panel_container's docstring).
    start = _worker_fact(
        "w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="sub_session", worker_session_id="sess-t",
    )
    complete = _worker_fact(
        "w:2", 2.0, 2, "worker_complete", "deleg-1",
        worker_session_id="sess-t", target_turn_id="prompt-abc",
    )
    container = build_subagent_panel_container([start, complete], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.target_ref.session_id == "sess-t"
    assert container.target_ref.turn_id == "prompt-abc"


def test_build_subagent_panel_container_target_turn_id_none_while_running():
    # No `worker_complete` fact yet (still running) — honest None, not
    # guessed, even though `worker_session_id` (a DIFFERENT field) is
    # already known.
    start = _worker_fact(
        "w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="sub_session", worker_session_id="sess-t",
    )
    container = build_subagent_panel_container([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.target_ref.session_id == "sess-t"
    assert container.target_ref.turn_id is None


def test_build_subagent_panel_container_sidecar_ref_mirrors_worker_session_id():
    start = _worker_fact(
        "w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker", worker_session_id="sess-side",
    )
    container = build_subagent_panel_container([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.sidecar_ref == "sess-side"


def test_build_subagent_panel_container_sidecar_ref_none_without_worker_session_id():
    start = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    container = build_subagent_panel_container([start], surface_id=SURFACE, turn_id=TURN, cv=1)
    assert container.sidecar_ref is None


def test_delegation_id_of_ignores_non_worker_interaction_nodes():
    assert delegation_id_of(_text("t1", 1.0, 1, "hi")) is None


def test_delegation_id_of_ignores_facts_missing_delegation_id():
    node = Node(
        cv=1, node_id="w:1", parent_id=None, turn_id=TURN, surface_id=SURFACE,
        kind=NodeKind.WORKER_INTERACTION, ts=1.0, seq=1, status=ContentStatus.COMPLETE,
        payload=WorkerInteractionPayload(fact_kind="worker_start", fact={}),
    )
    assert delegation_id_of(node) is None


def test_derive_turn_counts_one_renderable_item_per_worker_turn():
    # Full pipeline parity, mirroring test_derive_turn_counts_one_
    # renderable_item_per_subagent_turn: once worker facts are grouped
    # (as chat_adapter._build_turn_view now does before calling
    # derive_turn), the enclosing turn's own manifest counts the panel as
    # exactly ONE renderable BodyItem.
    start = _worker_fact("w:1", 1.0, 1, "worker_start", "deleg-1", panel_kind="worker")
    body_source, _extra = build_subagent_turns_for_panels(
        [start], surface_id=SURFACE, turn_id=TURN, cv=1,
    )
    turn = derive_turn(TURN, body_source, surface_id=SURFACE, cv=1)
    panel_items = [n for n in turn["body"].items if n.kind == NodeKind.WORKER_TURN]
    assert len(panel_items) == 1
    assert turn["turn"].child_manifest.renderable_child_count == 1


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
