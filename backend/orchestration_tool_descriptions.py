"""Single source of truth for the model-facing descriptions of the
session-orchestration tools (mssg / ask / delegate_task /
create_session / create_sub_session / create_worker).

All three provider runners import these constants so the descriptions cannot
drift per provider:
  - Claude  -> runner.py        (@tool builders)
  - Codex   -> runner_codex.py  (dynamic tool dicts)
  - Gemini  -> communicate_mcp.py (FastMCP stdio server)

Keep each description concise and lead with the ONE axis that disambiguates
the tool from its neighbours: wait-vs-detached, team-scoped-vs-any-session,
and fork-for-review. A description-parity test locks these against drift.
"""
from __future__ import annotations

from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN,
)

_PROVIDER_SELECTOR_NOTE = (
    " Leave provider/model/reasoning unset unless a different one is truly required."
)

MSSG_DESCRIPTION = (
    "Send a queued message to one target and return after the backend accepts "
    "it. Target with exactly one of target_session_id, "
    "target_worker_id, or target_worker_pool. Use for direct coordination or a "
    "worker's final report to its manager. For a reply you read inline, use ask "
    f"mode='{ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN}' instead. With "
    "target_worker_pool, pass pool_affinity_key to continue the same thread on "
    "the same pool worker."
)

ASK_DESCRIPTION = (
    "Message an existing session. mode='"
    f"{ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN}' waits for its turn to finish "
    "and returns the reply inline; mode='"
    f"{ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC}' continues it and returns "
    "immediately while expecting a later mssg back. run_mode='direct' (default) "
    "runs the real session; "
    "run_mode='fork' runs an ISOLATED branch that does NOT mutate the session's "
    "durable context — use fork for audits/reviews/checks (set ephemeral=true to "
    "discard the fork after). Do NOT fork to create a brand-new session. Unlike "
    "delegate_task, ask keeps the target explicit. Unlike the session-bridge "
    "delegate_to_session tool, ask has NO approval picker and cannot create a "
    "session — the target session must already exist — so use delegate_to_session "
    "when you need user consent or a brand-new session. With target_worker_pool, "
    "pass pool_affinity_key to continue the same thread on the same pool worker."
)

DELEGATE_TASK_DESCRIPTION = (
    "Hand a task to another session and keep working — DETACHED fire-and-forget "
    "that does NOT hold your turn open (unlike ask wait mode). The backend "
    "auto-routes (search for a fitting session or create one) unless you pass a "
    "known target_session_id to bypass routing. Use to offload heavy tangential / "
    "off-topic work so you stay focused. Not for reviews — use ask(run_mode='fork'). "
    "Distinct from the session-bridge delegate_to_session tool, which waits for the "
    "result." + _PROVIDER_SELECTOR_NOTE
)

CREATE_SESSION_DESCRIPTION = (
    "Create a fresh STANDALONE Better Agent session (no team roster entry, no "
    "approval); returns its session_id so you can send work to it with "
    "mssg/ask/delegate_task. orchestration_mode='team' only for complex tasks that "
    "need their own coordinator, otherwise 'native'. To add a session to your team's "
    "worker roster instead, use create_worker." + _PROVIDER_SELECTOR_NOTE
)

CREATE_SUB_SESSION_DESCRIPTION = (
    "Create a hidden native sub-session under your current session — no prompt is "
    "sent, and it does not appear as a sidebar session or team worker. Returns "
    "target_session_id; send work to it later with mssg or ask. Use to "
    "pre-provision a private helper session." + _PROVIDER_SELECTOR_NOTE
)

CREATE_WORKER_DESCRIPTION = (
    "Request a fresh TEAM worker session (team managers only). May show the user an "
    "approval card and BLOCKS until approved or denied. After success, send work to "
    "the returned worker_session_id with mssg/ask/delegate_task. For a standalone "
    "non-roster session, use create_session instead." + _PROVIDER_SELECTOR_NOTE
)

CHAT_DESCRIPTION = (
    "Post to and read from a shared team chat that every team session sees. "
    "Your id is taken from the session automatically. Pass a non-empty message to "
    "append it (stamped with your id); empty/whitespace message means read-only. "
    "Returns only the messages newer than YOUR last-read position, then advances "
    "your cursor — so each session independently sees what arrived since it last "
    "looked. The chat must already exist (create it with create_chat)."
)

CREATE_CHAT_DESCRIPTION = (
    "Create a shared team chat by chat_id. Once created, any team session can "
    "post/read it with chat, and it persists until delete_chat. Fails if the "
    "chat_id already exists."
)

DELETE_CHAT_DESCRIPTION = (
    "Permanently delete a shared team chat by chat_id. No-op (success=false) if "
    "the chat does not exist."
)

ENSURE_NAMED_WORKER_DESCRIPTION = (
    "Idempotently get-or-create a NAMED singleton worker by `worker:<name>` + cwd and "
    "return its agent_session_id. If a worker with that name and cwd already exists it "
    "is reused (created=false); otherwise a new one is provisioned with the given "
    "orchestration_mode and optional provision_prompt (created=true). Use this — not "
    "create_worker — when you want a STABLE, REUSABLE named worker reachable from any "
    "session (e.g. a single global cross-project worker). After it returns, delegate "
    "work with ask/mssg/delegate_task. Available to all sessions."
)

LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION = (
    "List selectable Better Agent providers with their available models and "
    "reasoning efforts. Inputs are optional fuzzy filters; omit all inputs to "
    "return every non-suspended provider."
)
