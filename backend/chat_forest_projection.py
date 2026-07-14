from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from canonical_event import CommittedFact, stable_node_id


@dataclass(frozen=True)
class PromptNode:
    id: str
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ExplanationNode:
    id: str
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class WorkNode:
    id: str
    kind: str
    payload: dict[str, Any]


@dataclass
class PromptTree:
    id: str
    prompt: PromptNode
    turn_id: str | None = None
    explanations: list[ExplanationNode] = field(default_factory=list)
    work: list[WorkNode] = field(default_factory=list)
    status: str = "complete"
    queued: bool = False
    partial: bool = False
    has_late_output: bool = False
    events_collapsed_by_default: bool = True
    prompt_text_collapsed_by_default: bool = False
    collapsed_preview: str = ""


@dataclass(frozen=True)
class ChatForest:
    root_id: str
    root_generation: int
    canonical_through_seq: int
    trees: list[PromptTree]


class ChatForestProjector:
    def project(self, root_id: str, facts: Iterable[CommittedFact]) -> ChatForest:
        rows = list(facts)
        generations = {row.fact.root_generation for row in rows}
        if len(generations) > 1:
            raise ValueError("chat forest cannot mix root generations")
        root_generation = next(iter(generations), 0)
        selected = self._select_snapshots(rows)
        prompts: dict[str, PromptTree] = {}
        order: list[str] = []
        default_prompt = "__system__"

        def tree_for(prompt_id: str | None) -> PromptTree:
            key = prompt_id or default_prompt
            tree = prompts.get(key)
            if tree is not None:
                return tree
            prompt = PromptNode(
                id=stable_node_id("prompt", root_id, key),
                text="",
                payload={"kind": "system_root" if key == default_prompt else "missing_prompt"},
            )
            tree = PromptTree(id=stable_node_id("prompt_tree", root_id, key), prompt=prompt)
            prompts[key] = tree
            order.append(key)
            return tree

        tools: dict[str, tuple[PromptTree, int]] = {}
        for row in selected:
            fact = row.fact
            payload = fact.payload
            prompt_id = payload.get("prompt_message_id")
            if fact.payload_type == "user_prompt":
                message_id = str(payload.get("message_id") or fact.fact_id)
                tree = tree_for(message_id)
                tree.prompt = PromptNode(
                    id=stable_node_id("prompt", root_id, message_id),
                    text=str(payload.get("text") or ""), payload=payload,
                )
                tree.turn_id = fact.turn_id or message_id
                tree.queued = bool(payload.get("queued"))
                tree.status = "queued" if tree.queued else tree.status
                continue
            tree = tree_for(str(prompt_id) if prompt_id else None)
            if fact.payload_type == "assistant_output":
                text = str(payload.get("text") or "")
                tree.explanations.append(ExplanationNode(
                    id=stable_node_id("explanation", fact.source_stream_id, fact.source_event_id),
                    text=text, payload=payload,
                ))
                tree.collapsed_preview = text
                tree.partial = tree.partial or fact.update_semantics == "incomplete_snapshot" or bool(payload.get("partial"))
                tree.has_late_output = tree.has_late_output or bool(payload.get("late"))
            elif fact.payload_type == "steer_prompt":
                tree.work.append(WorkNode(
                    id=stable_node_id("steer", fact.source_stream_id, fact.source_event_id),
                    kind="steer_prompt", payload=payload,
                ))
            elif fact.payload_type == "tool_call":
                tool_id = str(payload.get("tool_use_id") or fact.fact_id)
                node = WorkNode(
                    id=stable_node_id("tool", fact.source_stream_id, tool_id),
                    kind="tool_interaction", payload={"call": payload},
                )
                tree.work.append(node)
                tools[tool_id] = (tree, len(tree.work) - 1)
            elif fact.payload_type == "tool_result":
                tool_id = str(payload.get("tool_use_id") or "")
                match = tools.get(tool_id)
                if match:
                    owner, index = match
                    current = owner.work[index]
                    owner.work[index] = WorkNode(current.id, current.kind, {**current.payload, "result": payload})
                else:
                    tree.work.append(WorkNode(
                        id=stable_node_id("orphan_tool_result", fact.fact_id),
                        kind="orphan_tool_result", payload=payload,
                    ))
            elif fact.payload_type in {"worker_start", "worker_event", "worker_complete", "worker_final", "todos_snapshot"}:
                parent_tool = str(payload.get("parent_tool_use_id") or "")
                child_source = str(payload.get("child_source") or payload.get("child_id") or fact.source_stream_id)
                tree.work.append(WorkNode(
                    id=stable_node_id("worker", root_id, parent_tool, child_source, fact.source_event_id),
                    kind=fact.payload_type, payload=payload,
                ))
            elif fact.payload_type in {"turn_failed", "turn_cancelled", "turn_complete"}:
                tree.status = {
                    "turn_failed": "failed", "turn_cancelled": "cancelled", "turn_complete": "complete",
                }[fact.payload_type]
        through = max((row.canonical_seq for row in rows), default=0)
        return ChatForest(root_id=root_id, root_generation=root_generation, canonical_through_seq=through, trees=[prompts[key] for key in order])

    @staticmethod
    def _select_snapshots(rows: list[CommittedFact]) -> list[CommittedFact]:
        latest: dict[tuple[str, str], CommittedFact] = {}
        standalone: list[CommittedFact] = []
        for row in rows:
            fact = row.fact
            if fact.update_semantics in {"snapshot", "final", "incomplete_snapshot"} and fact.payload_type in {"assistant_output", "worker_event"}:
                key = fact.source_stream_id, fact.source_event_id
                current = latest.get(key)
                if current is None or current.fact.source_order.key() < fact.source_order.key():
                    latest[key] = row
                continue
            standalone.append(row)
        selected_ids = {row.fact.fact_id for row in latest.values()}
        selected_ids.update(row.fact.fact_id for row in standalone)
        return [row for row in rows if row.fact.fact_id in selected_ids]
