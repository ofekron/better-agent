"""Wire serialization of the formal chat tree.

Converts `chat_projector.project_chat` output (`chat_models.Chat`) into
the snake_case JSON shape the frontend's `src/chat/parseProjection.ts`
accepts — the contract locked by
`test-contracts/chat-panel/v1/canonical-session.json` and verified from
both sides by `scripts/test_chat_projector_contract.py` (producer) and
`frontend/tests/chat-canonical-pure.test.ts` (consumer).
"""
from __future__ import annotations

from typing import Any

from chat_models import (
    Chat,
    Explanation,
    ModelChange,
    Result,
    ScopedTurn,
    SteeringMessage,
    Turn,
)


def result_to_wire(value: Result | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"type": value.type, "part_ids": list(value.part_ids), "text": value.text}


def body_item_to_wire(value: Explanation | SteeringMessage | ScopedTurn) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    stack: list[tuple[Any, dict[str, Any]]] = [(value, projected)]
    while stack:
        item, target = stack.pop()
        if isinstance(item, Explanation):
            target.update({
                "type": "Explanation", "text": item.text,
                "text_event_ids": list(item.text_event_ids),
                "item_ids": list(item.item_ids),
            })
            continue
        if isinstance(item, SteeringMessage):
            target.update({"type": "SteeringMessage", "id": item.id, "text": item.text})
            continue
        if not isinstance(item, ScopedTurn):
            raise TypeError(f"unsupported body item: {type(item).__name__}")
        target.update({
            "type": item.type, "id": item.id, "prompt": item.prompt.text,
            "body": [{} for _ in item.body], "result": result_to_wire(item.result),
            "children": list(item.children),
        })
        for child, child_target in reversed(list(zip(item.body, target["body"]))):
            stack.append((child, child_target))
    return projected


def chat_to_wire(chat: Chat) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in chat.items:
        if isinstance(item, ModelChange):
            items.append({"type": "ModelChange", "id": item.id, "before_turn": item.before_turn})
            continue
        if not isinstance(item, Turn):
            raise TypeError(f"unsupported chat item: {type(item).__name__}")
        items.append({
            "type": "Turn",
            "id": item.id,
            "prompt": item.prompt.id,
            "body": [body_item_to_wire(body) for body in item.body],
            "result": result_to_wire(item.result),
        })
    return items
