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
) -> RenderedChat:
    """Render one root's wire facts + session snapshot into chat-tree items."""
    adapted = adapt_chat_inputs(facts, session)
    chat = project_chat(
        adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION,
    )
    return RenderedChat(chat_to_wire(chat), adapted)
