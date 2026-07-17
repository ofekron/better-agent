"""Single render path: canonical facts + session snapshot -> chat tree.

The one implementation both the durable read path (`bff_chat_tree`) and
the ephemeral current-turn cache (`bff_current_turn_cache`) call. Keeping
it here — not duplicated in each caller — is the guard against the
two-pipeline rendering divergence this project has already had to fix
once, just relocated inside one process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from chat_canonical_adapter import AdaptedChatInputs, adapt_chat_inputs
from chat_models import CHAT_SCHEMA_VERSION
from chat_projector import project_chat
from chat_tree_wire import chat_to_wire


@dataclass(frozen=True)
class RenderedChat:
    items: list[dict[str, Any]]
    adapted: AdaptedChatInputs


def render_chat(
    facts: Sequence[Mapping[str, Any]],
    session: Mapping[str, Any],
    *,
    pane_id: str | None = None,
) -> RenderedChat:
    """Render one pane's wire facts + session snapshot into chat-tree items.

    A pane is one session-tree node (the root or a fork). Message seqs are
    per-node counters, so the projector must only ever see one pane's
    messages; events of other panes drop out naturally because their turn
    is not among the pane's prompts.
    """
    root_pane = str(session.get("id") or "")
    pane = pane_id or root_pane
    adapted = adapt_chat_inputs(facts, session)
    # A message without context_id attributes to the ROOT pane (fail
    # closed: never default into whatever pane was requested).
    pane_messages = [
        message for message in adapted.messages
        if str(message.get("context_id") or root_pane) == pane
    ]
    chat = project_chat(
        pane_messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION,
    )
    return RenderedChat(chat_to_wire(chat), adapted)
