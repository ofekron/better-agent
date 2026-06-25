from __future__ import annotations

import json
from pathlib import Path


# Bump when Claude's derived render tree changes shape, so sessions digested
# under an older pipeline re-digest from their session jsonl on next startup
# (run dir + native jsonl retained; reconciled.marker is version-stamped and
# a mismatch re-opens a run for re-digest). v2: re-ingestion now repairs
# `updated_at` to the session's real last-activity timestamp, undoing the
# spurious bumps that mis-ordered the sidebar.
CLAUDE_INGESTION_VERSION = 2
# Bump when Codex's derived render tree changes shape, so sessions digested
# under an older normalizer re-digest from their rollout on next startup
# (run dir + native rollout retained; reconciled.marker is version-stamped
# and a mismatch re-opens a run for re-digest). v3: drop event_msg.agent_message
# echo duplicates, stamp real rollout timestamps, hide encrypted-only reasoning.
# v4: stop rendering token_count/task_complete cards; route context_window
# into the existing context-window UI channel via the complete envelope.
# v5: re-ingestion now repairs `updated_at` to the session's real last-activity
# timestamp, undoing the spurious bumps that mis-ordered the sidebar.
# v6: recover Codex native subagent panels from parent wait/notification rows
# when live ingestion missed persisting child rollout sources.
CODEX_INGESTION_VERSION = 6
# Bump when agy's recovery digest changes shape. v2: recovery now replays agy's
# session_events.jsonl through the gemini-family reader instead of the Claude
# parser, so runs reconciled under v1 (empty/partial render) re-digest on the
# next startup.
AGY_INGESTION_VERSION = 2
# Same bug/fix as agy: copilot is a gemini-family provider whose runner writes
# session_events.jsonl. v2: recovery now replays it through the gemini-family
# reader instead of the Claude parser, so runs reconciled under v1 re-digest.
COPILOT_INGESTION_VERSION = 2


def current_ingestion_version(provider_kind: str | None) -> int:
    if provider_kind == "codex":
        return CODEX_INGESTION_VERSION
    if provider_kind == "claude":
        return CLAUDE_INGESTION_VERSION
    if provider_kind == "agy":
        return AGY_INGESTION_VERSION
    if provider_kind == "copilot":
        return COPILOT_INGESTION_VERSION
    return 1


def marker_matches_current(path: Path, provider_kind: str | None) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        data.get("provider_kind") == provider_kind
        and data.get("ingestion_version") == current_ingestion_version(provider_kind)
    )


def write_marker(path: Path, provider_kind: str | None) -> None:
    data = {
        "provider_kind": provider_kind,
        "ingestion_version": current_ingestion_version(provider_kind),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
