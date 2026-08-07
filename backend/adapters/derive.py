"""Pure chat-panel.md grammar derivation over list[Node] (ADR 0006). No I/O,
no bus. Mirrors chat-panel.md's resolveResult / deriveBodyItems / deriveTurn
render algorithm section, operating on backend.surface_contract.nodes.Node
instead of frontend render objects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from backend.surface_contract.identity import NodeId, SurfaceId, TurnId
from backend.surface_contract.nodes import (
    SUBAGENT_TURN_KINDS,
    ChildManifest,
    Node,
    NodeKind,
    ResultKind,
)

# Items directly preserved in place by deriveBodyItems (never folded into
# an Explanation's action list).
_PRESERVED_IN_PLACE = SUBAGENT_TURN_KINDS | {NodeKind.STEERING_MESSAGE}

# Kinds that never count as a turn's/explanation's renderable content:
# structural containers, the prompt itself, and in-place notices that
# render at their occurrence rather than as expandable children
# (chat-panel.md: "Diagnostic ... never a ChatItem"; "A lifecycle-only
# turn has zero renderable children and no three-dot process control").
_NON_RENDERABLE_KINDS = frozenset(
    {
        NodeKind.TURN,
        NodeKind.EXPLANATION,
        NodeKind.TYPED_PROMPT,
        NodeKind.LIFECYCLE_NOTICE,
        NodeKind.DIAGNOSTIC,
    }
)


def _ordered(nodes: list[Node]) -> list[Node]:
    return sorted(nodes, key=lambda n: (n.ts, n.seq))


def _is_marked_final(n: Node) -> bool:
    return n.kind == NodeKind.RESULT and n.payload is not None and n.payload.result_kind == ResultKind.PROVIDER


def resolve_result(nodes: list[Node]) -> tuple[list[Node], list[Node]]:
    """resolveResult(turn) — returns (result_nodes, consumed_source_items)."""
    items = _ordered(nodes)
    if not items:
        return [], []

    marked = [n for n in items if _is_marked_final(n)]
    if marked:
        marked_ids = {n.node_id for n in marked}
        associated_text = [
            n
            for n in items
            if n.kind == NodeKind.ASSISTANT_TEXT and n.parent_id in marked_ids
        ]
        result_items = _ordered(marked + associated_text)
        return result_items, result_items

    trailing: list[Node] = []
    for n in reversed(items):
        if n.kind != NodeKind.ASSISTANT_TEXT:
            break
        trailing.insert(0, n)
    if trailing:
        return trailing, trailing

    # A turn's "result" fallback is never a BodyItem in its own right —
    # chat-panel.md: SubAgentTurn is a BodyItem embedding a full nested
    # Turn, never the turn's terminal result, and the same holds for
    # SteeringMessage. Skip trailing `_PRESERVED_IN_PLACE` items and fall
    # back to the last genuine content item; if the turn is made ENTIRELY
    # of preserved-in-place items (e.g. a turn that is just one subagent
    # call), there is no synthesizable result at all — that item stays a
    # plain BodyItem instead of being cannibalized into the result slot.
    fallback_candidates = [n for n in items if n.kind not in _PRESERVED_IN_PLACE]
    if not fallback_candidates:
        return [], []
    final_item = fallback_candidates[-1]
    return [final_item], [final_item]


def _explanation_node_id(first_child: Node) -> NodeId:
    return f"explanation:{first_child.node_id}"


@dataclass(frozen=True)
class DerivedBody:
    """derive_body's return: `items` is the ordered flat body (Explanation
    summary Nodes merged with in-place-preserved Nodes, chat-panel.md's
    deriveBodyItems result); `membership` maps each Explanation's node_id
    to the raw member Nodes it wraps, in order, so callers can attach
    parent_id onto both the Explanation and its members."""

    items: tuple[Node, ...]
    membership: dict[NodeId, tuple[Node, ...]]


def derive_body(nodes: list[Node], *, surface_id: SurfaceId, turn_id: TurnId, cv: int) -> DerivedBody:
    """deriveBodyItems — partitions items at each AssistantText boundary,
    preserving SubAgentTurn/SteeringMessage items in place; each partition
    becomes a synthetic Explanation Node wrapping its (text, actions)
    children."""
    items = _ordered(nodes)
    preserved = [n for n in items if n.kind in _PRESERVED_IN_PLACE]
    remaining = [n for n in items if n.kind not in _PRESERVED_IN_PLACE]

    body: list[Node] = []
    membership: dict[NodeId, tuple[Node, ...]] = {}
    partition: list[Node] = []
    still_leading = True

    def flush() -> None:
        if not partition:
            return
        first = partition[0]
        explanation_id = _explanation_node_id(first)
        renderable = sum(1 for n in partition if n.kind not in _NON_RENDERABLE_KINDS)
        body.append(
            Node(
                cv=cv,
                node_id=explanation_id,
                parent_id=None,
                turn_id=turn_id,
                surface_id=surface_id,
                kind=NodeKind.EXPLANATION,
                ts=first.ts,
                seq=first.seq,
                status=None,
                payload=None,
                child_manifest=ChildManifest(renderable_child_count=renderable, has_children=bool(partition)),
            )
        )
        membership[explanation_id] = tuple(partition)
        partition.clear()

    for n in remaining:
        if n.kind == NodeKind.ASSISTANT_TEXT:
            if partition and not still_leading:
                flush()
                still_leading = True
        else:
            still_leading = False
        partition.append(n)
    flush()

    merged = _ordered(body + preserved)
    return DerivedBody(items=tuple(merged), membership=membership)


def child_manifest(nodes: list[Node]) -> ChildManifest:
    renderable = sum(1 for n in nodes if n.kind not in _NON_RENDERABLE_KINDS)
    return ChildManifest(renderable_child_count=renderable, has_children=bool(nodes))


def attach_body_items(
    body: DerivedBody, *, parent_id: NodeId,
) -> tuple[list[Node], dict[NodeId, Node]]:
    """Stamp `parent_id` onto every one of `body`'s top-level items —
    an Explanation node AND its members, or a preserved-in-place item
    directly — collecting everything touched into an index. The single
    "attach a derived body to its container" step, shared by
    `build_subagent_turns` (container = a subagent turn) and
    `chat_adapter._build_turn_view` (container = the turn itself) rather
    than each re-implementing the same Explanation/preserved-item
    dispatch.

    Returns `(items, index)`: `items` is `body.items` with `parent_id`
    stamped (same order); `index` is every node_id -> Node touched,
    including Explanation members (whose OWN parent_id points at their
    Explanation, not `parent_id`)."""
    items: list[Node] = []
    index: dict[NodeId, Node] = {}
    for item in body.items:
        if item.kind == NodeKind.EXPLANATION:
            exp = replace(item, parent_id=parent_id)
            index[exp.node_id] = exp
            for member in body.membership[exp.node_id]:
                m = replace(member, parent_id=exp.node_id)
                index[m.node_id] = m
            items.append(exp)
        else:
            pn = replace(item, parent_id=parent_id)
            index[pn.node_id] = pn
            items.append(pn)
    return items, index


def build_subagent_turns(
    nodes: list[Node],
    is_sidechain: dict[NodeId, bool],
    *,
    surface_id: SurfaceId,
    turn_id: TurnId,
    cv: int,
) -> tuple[list[Node], dict[NodeId, Node]]:
    """chat-panel.md grammar: SubAgentTurn is a BodyItem embedding a full
    nested Turn — sidechain content must never flat-merge into its
    parent's body. (The bug this closes: neither `derive_body` nor
    `derive_turn` had any concept of nesting, so every sidechain message
    became its own top-level item, chronologically interleaved with the
    turn's own narrative — a single Task-tool invocation could inflate a
    turn from a handful of items to thousands.)

    Segregates every maximal sidechain-rooted subtree in `nodes` into its
    own `NodeKind.NATIVE_SUBAGENT_TURN` structural node, anchored at the
    spawning tool_interaction — the node a sidechain root's `parent_id`
    resolves to via `parent_tool_use_id` (see `normalize.resolve_parents`;
    `is_sidechain` is what distinguishes this from an ordinary tool_use/
    tool_result pairing, which never reaches here — pairing already
    consumed those before this runs). Detected structurally (is_sidechain
    node whose resolved parent is TOOL_INTERACTION-kind), not by depth —
    Claude Code's own `isSidechain` tag doesn't encode nesting depth — so
    a sidechain spawning a sidechain (a nested Task tool_use performed BY
    a subagent) is found the same way, one level deeper, via recursion.

    Call BEFORE `derive_turn`/`derive_body`: a segregated SubAgentTurn
    node is `NodeKind.NATIVE_SUBAGENT_TURN`, already in
    `SUBAGENT_TURN_KINDS`/`_PRESERVED_IN_PLACE`, so the pre-existing
    "preserved in place, one BodyItem" machinery there requires no changes
    to render it correctly once given properly-segregated input.

    Returns `(kept, extra_index)`:
      `kept` — `nodes` with every sidechain node removed and one
        NATIVE_SUBAGENT_TURN node inserted per top-level spawning anchor
        found in THIS node list (ts/seq = its subtree's earliest member,
        so chronological ordering / derive_body's partitioning stays
        correct); `parent_id` unset — same convention `derive_body`'s own
        Explanation nodes use, stamped by the caller.
      `extra_index` — node_id -> Node for every node NOT in `kept`: every
        subtree member at every nesting depth, `parent_id` fully stamped
        (a subagent turn's own children point to it; a nested
        Explanation's members point to that Explanation; a nested
        subagent turn points to ITS enclosing subagent turn) — merge into
        the turn's index so `children()` serves any of them one level at
        a time, the SAME generic parent_id scan Explanation members
        already use — never flattened further than that.
    """
    by_id = {n.node_id: n for n in nodes}
    children_by_parent: dict[NodeId, list[Node]] = {}
    for n in nodes:
        if n.parent_id is not None:
            children_by_parent.setdefault(n.parent_id, []).append(n)

    def _is_root(n: Node) -> bool:
        if not is_sidechain.get(n.node_id, False):
            return False
        parent = by_id.get(n.parent_id) if n.parent_id is not None else None
        return parent is not None and parent.kind == NodeKind.TOOL_INTERACTION

    all_roots = [n for n in nodes if _is_root(n)]
    if not all_roots:
        return nodes, {}
    all_root_ids = {n.node_id for n in all_roots}

    def _is_descendant_of_another_root(n: Node) -> bool:
        # Claude Code's `isSidechain` tag carries no depth — a node's OWN
        # root-ness (`_is_root`) can't tell "top-level sidechain" apart
        # from "a deeper nested sidechain's root" on its own; walking the
        # ancestor chain for ANOTHER root does. Only a TOP-level root
        # (none of its ancestors is itself a root) gets its own anchor at
        # THIS level — a nested one is left for the recursive call on its
        # enclosing root's subtree to find, one level deeper.
        current: Node | None = n
        seen: set[NodeId] = set()
        while current is not None and current.parent_id is not None and current.parent_id not in seen:
            seen.add(current.parent_id)
            parent = by_id.get(current.parent_id)
            if parent is None:
                return False
            if parent.node_id in all_root_ids:
                return True
            current = parent
        return False

    roots_by_anchor: dict[NodeId, list[Node]] = {}
    for n in all_roots:
        if not _is_descendant_of_another_root(n):
            roots_by_anchor.setdefault(n.parent_id, []).append(n)

    def _collect_subtree(root: Node, seen: set[NodeId]) -> list[Node]:
        # Iterative (explicit stack), NOT recursive: a real sidechain
        # conversation chains via `parentUuid` message-by-message, so its
        # "tree" is often just one long linear chain — a per-node Python
        # call-stack recursion here would blow the recursion limit on
        # exactly the deep sidechains this function exists to bound.
        out: list[Node] = []
        stack = [root]
        while stack:
            current = stack.pop()
            if current.node_id in seen:
                continue
            seen.add(current.node_id)
            out.append(current)
            stack.extend(children_by_parent.get(current.node_id, []))
        return out

    extra_index: dict[NodeId, Node] = {}
    sidechain_node_ids: set[NodeId] = set()
    subagent_nodes: list[Node] = []
    for anchor, roots in roots_by_anchor.items():
        seen: set[NodeId] = set()
        subtree: list[Node] = []
        for root in sorted(roots, key=lambda n: (n.ts, n.seq)):
            subtree.extend(_collect_subtree(root, seen))
        sidechain_node_ids.update(seen)

        # Recurse: `subtree` may itself contain a deeper-nested sidechain
        # — peel it off before deriving THIS level's body, so it nests as
        # its own NATIVE_SUBAGENT_TURN one level down rather than being
        # flattened into this level's Explanation partitioning.
        inner_kept, inner_extra = build_subagent_turns(
            subtree, is_sidechain, surface_id=surface_id, turn_id=turn_id, cv=cv,
        )
        # Merge the recursive call's index FIRST: any nested subagent-turn
        # node it returned is still parent_id-unstamped at this point
        # (same "caller stamps it" convention `kept` uses) — the per-item
        # loop below overwrites it with the correctly-parented version
        # once it's discovered again as a `body.items` entry.
        extra_index.update(inner_extra)
        subagent_node_id = f"subagent:{anchor}"
        body = derive_body(inner_kept, surface_id=surface_id, turn_id=turn_id, cv=cv)
        final_items, body_index = attach_body_items(body, parent_id=subagent_node_id)
        extra_index.update(body_index)

        earliest = min(subtree, key=lambda n: (n.ts, n.seq))
        subagent_node = Node(
            cv=cv, node_id=subagent_node_id, parent_id=None, turn_id=turn_id,
            surface_id=surface_id, kind=NodeKind.NATIVE_SUBAGENT_TURN,
            ts=earliest.ts, seq=earliest.seq, status=None, payload=None,
            child_manifest=child_manifest(final_items),
        )
        subagent_nodes.append(subagent_node)
        extra_index[subagent_node_id] = subagent_node

    kept = [n for n in nodes if n.node_id not in sidechain_node_ids] + subagent_nodes
    return kept, extra_index


def derive_turn(
    turn_id: TurnId, nodes: list[Node], *, surface_id: SurfaceId, cv: int
) -> dict[str, object]:
    """deriveTurn(turn) — {turn, prompt, body, result} per chat-panel.md."""
    items = _ordered(nodes)
    prompt = next((n for n in items if n.kind == NodeKind.TYPED_PROMPT), None)

    non_prompt = [n for n in items if n.kind != NodeKind.TYPED_PROMPT]
    result_nodes, result_source_items = resolve_result(non_prompt)
    result_ids = {n.node_id for n in result_source_items}
    body_source = [n for n in non_prompt if n.node_id not in result_ids]

    body = derive_body(body_source, surface_id=surface_id, turn_id=turn_id, cv=cv)

    manifest = child_manifest(list(body.items) + result_nodes)
    turn_node = Node(
        cv=cv,
        node_id=f"turn:{turn_id}",
        parent_id=None,
        turn_id=turn_id,
        surface_id=surface_id,
        kind=NodeKind.TURN,
        ts=prompt.ts if prompt else (items[0].ts if items else 0.0),
        seq=prompt.seq if prompt else (items[0].seq if items else 0),
        status=None,
        payload=None,
        child_manifest=manifest,
    )

    return {
        "turn": turn_node,
        "prompt": prompt,
        "body": body,
        "result": result_nodes,
    }
