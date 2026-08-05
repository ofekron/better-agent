"""Pure chat-panel.md grammar derivation over list[Node] (ADR 0006). No I/O,
no bus. Mirrors chat-panel.md's resolveResult / deriveBodyItems / deriveTurn
render algorithm section, operating on backend.surface_contract.nodes.Node
instead of frontend render objects.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    final_item = items[-1]
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
