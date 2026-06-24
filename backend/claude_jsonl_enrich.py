"""Claude jsonl line enrichment + sub-agent tracking.

Pure-function pipeline shared by live tailing (`jsonl_tailer`) and
batch replay (`run_recovery`). Lives here, not on `provider_claude`,
to break the `jsonl_tailer ↔ provider_claude` lazy-import cycle —
this is a data-shape concern, not a provider-driver concern.

Enrichment is additive — original claude fields are preserved so the
frontend can still read the raw claude shape. Added field:

  - `parent_tool_use_id` — for `isSidechain=True` messages, the
    nearest ancestor message's `tool_use` id, i.e. the enclosing
    sub-agent's Task/Agent call id. Walks the parentUuid chain
    using the per-run uuid→tool_use_ids / uuid→parent_uuid maps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


def _enrich_claude_line(
    parsed: dict,
    uuid_to_tool_use_ids: dict[str, list[str]],
    uuid_to_parent_uuid: dict[str, str],
) -> dict:
    """Augment a parsed claude jsonl line with computed fields.

    Mutates the two tracking dicts in place so subsequent calls can walk
    the DAG for this run.
    """
    enriched = dict(parsed)

    msg_uuid = parsed.get("uuid")
    parent_uuid = parsed.get("parentUuid")
    is_sidechain = parsed.get("isSidechain", False)

    if msg_uuid and parent_uuid:
        uuid_to_parent_uuid[msg_uuid] = parent_uuid

    # Record tool_use ids found on this message for later DAG lookups.
    content = (parsed.get("message") or {}).get("content")
    tool_use_ids_on_this_msg: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                bid = block.get("id")
                if bid:
                    tool_use_ids_on_this_msg.append(bid)
    if msg_uuid and tool_use_ids_on_this_msg:
        uuid_to_tool_use_ids[msg_uuid] = tool_use_ids_on_this_msg

    # Resolve parent_tool_use_id for sidechain messages only.
    parent_tool_use_id: Optional[str] = None
    if is_sidechain and parent_uuid:
        cursor: Optional[str] = parent_uuid
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            ids = uuid_to_tool_use_ids.get(cursor)
            if ids:
                # Use the LAST tool_use id on the enclosing assistant
                # message (matches the web_claude_provider convention).
                parent_tool_use_id = ids[-1]
                break
            cursor = uuid_to_parent_uuid.get(cursor)

    if parent_tool_use_id:
        enriched["parent_tool_use_id"] = parent_tool_use_id
    return enriched


# ============================================================================
# Subagent registry — shared across the parent tailer + all sub-tailers
# for one run, so nested Agent calls also nest correctly.
# ============================================================================
_TOOL_NAME_TYPES = frozenset({"Agent", "Task"})


@dataclass
class _PendingAgent:
    tool_use_id: str
    subagent_type: str
    description: str


class _SubagentRegistry:
    """FIFO registry of unmatched `Agent`/`Task`/`Workflow` tool_uses seen
    across all tailers for one run. Subagent files (`agent-<id>.meta.json`)
    are matched against pending entries by `(subagent_type, description)`.

    Workflow tool_uses are matched FIFO when a `subagents/workflows/wf_<id>/`
    directory appears — workflow agent metas lack toolUseId/description so
    the exact-match claim path cannot bind them.
    """

    def __init__(self) -> None:
        self._pending: list[_PendingAgent] = []
        # agentId → parent_tool_use_id, kept for crash-recovery / late
        # rebinding (currently unused but cheap).
        self._bound: dict[str, str] = {}
        # workflow run_id → parent Workflow tool_use_id.
        self._workflow_bindings: dict[str, str] = {}

    def register(self, tool_use_id: str, subagent_type: str, description: str) -> None:
        if not tool_use_id or not subagent_type:
            return
        self._pending.append(
            _PendingAgent(tool_use_id, subagent_type, description or "")
        )

    def claim(self, agent_type: str, description: str) -> Optional[str]:
        """Pop the first pending entry whose (subagent_type, description)
        matches; return None if nothing matches.

        Matching rules (ordered):

          1. Exact match on both (subagent_type, description).
          2. If the registered subagent_type is a tool-name fallback
             ("Agent" or "Task"), it is treated as a wildcard — any
             agent_type from the meta file matches, and only description
             is compared. This handles named subagents that omit
             ``subagent_type`` from their input (e.g. ``mode:
             bypassPermissions`` agents): ``register`` falls back to the
             tool name ("Agent"), but the meta file carries the real
             ``agentType`` (e.g. "general-purpose").

        INVARIANT: no type-only fallback. When ``_replay_subagents``
        runs in a recovery slice whose registry doesn't contain every
        Agent tool_use that ever spawned a sidecar in this claude
        session's dir, an unmatched meta MUST stay unclaimed — not
        steal a same-type pending entry meant for a different sidecar
        whose description matched exactly. A previous fallback by
        ``subagent_type`` alone caused real ingest losses: a meta
        whose parent Agent call was outside the slice stole the only
        remaining pending tool_use_id, leaving the actually-matching
        meta with an empty registry and dropped its events entirely.
        """
        for i, p in enumerate(self._pending):
            if p.subagent_type == agent_type and p.description == description:
                self._pending.pop(i)
                return p.tool_use_id
        # Fallback: description-only match when registered type is a
        # tool-name ("Agent" / "Task") — the real agentType is in the
        # meta file, not in the tool input.
        for i, p in enumerate(self._pending):
            if p.subagent_type in _TOOL_NAME_TYPES and p.description == description:
                self._pending.pop(i)
                return p.tool_use_id
        return None

    def claim_workflow(self, run_id: str) -> Optional[str]:
        """Pop the first pending Workflow entry and bind it to run_id.

        Returns the tool_use_id or None if no pending Workflow exists.
        FIFO is correct because the CLI creates one wf_<id> dir per
        Workflow invocation, in order, and meta files are discovered
        in the same order by the polling watcher.
        """
        for i, p in enumerate(self._pending):
            if p.subagent_type == "Workflow":
                entry = self._pending.pop(i)
                self._workflow_bindings[run_id] = entry.tool_use_id
                return entry.tool_use_id
        return None

    def get_workflow_parent(self, run_id: str) -> Optional[str]:
        """Return the tool_use_id bound to a workflow run_id."""
        return self._workflow_bindings.get(run_id)


def register_agent_tool_uses(parsed: dict, registry: _SubagentRegistry) -> None:
    """Register Agent/Task tool_uses from a parsed claude line.

    Called after enrichment so the subagent registry can match meta
    files to their enclosing tool_use when subagent jsonls appear."""
    msg = parsed.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name not in ("Agent", "Task", "Workflow"):
            continue
        tool_use_id = block.get("id")
        inp = block.get("input") or {}
        subagent_type = inp.get("subagent_type") or name
        description = inp.get("description") or ""
        if isinstance(tool_use_id, str) and isinstance(subagent_type, str):
            registry.register(tool_use_id, subagent_type, description)


def enrich_jsonl_line(
    raw_line: str,
    uuid_to_tool_use_ids: dict[str, list[str]],
    uuid_to_parent_uuid: dict[str, str],
    subagent_registry: _SubagentRegistry,
    parent_tool_use_id: Optional[str] = None,
) -> Optional[dict]:
    """Parse + enrich one jsonl line. Returns wrapped event dict or None.

    Shared enrichment pipeline for both live tailing (ClaudeJsonlTailer)
    and batch replay (run_recovery). Produces:
        {"type": "agent_message", "data": <enriched_dict>}
    or None if the line is empty / unparseable / not a dict.
    """
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        enriched = _enrich_claude_line(
            parsed, uuid_to_tool_use_ids, uuid_to_parent_uuid,
        )
    except Exception:
        enriched = parsed
    if parent_tool_use_id:
        enriched["parent_tool_use_id"] = parent_tool_use_id
    register_agent_tool_uses(parsed, subagent_registry)
    return {"type": "agent_message", "data": enriched}
