"""Single source of truth for the model-facing descriptions of the
session-orchestration tools (mssg / async / ask / delegate_task /
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

_PROVIDER_SELECTOR_NOTE = (
    " Leave provider/model/reasoning unset unless a different one is truly required."
)

MSSG_DESCRIPTION = (
    "Send a queued message to one target and BLOCK until it finishes — its "
    "completion joins your turn. Target with exactly one of target_session_id, "
    "target_worker_id, or target_worker_pool. Use for direct coordination or a "
    "worker's final report to its manager. For a reply you read inline, use ask "
    "instead; to fire-and-forget without waiting, use delegate_task."
)

ASYNC_DESCRIPTION = (
    "Send a queued message to one target and return immediately. Target with "
    "exactly one of target_session_id, target_worker_id, or target_worker_pool. "
    "The target is expected to report back later with mssg to the sender; use "
    "this for async worker-pool style work where you do not want to hold your "
    "current turn open."
)

ASK_DESCRIPTION = (
    "Message an existing session and WAIT for its turn to finish, returning that "
    "session's reply inline. run_mode='direct' (default) runs the real session; "
    "run_mode='fork' runs an ISOLATED branch that does NOT mutate the session's "
    "durable context — use fork for audits/reviews/checks (set ephemeral=true to "
    "discard the fork after). Do NOT fork to create a brand-new session. Unlike "
    "delegate_task (detached), ask is synchronous and returns the answer this turn."
)

DELEGATE_TASK_DESCRIPTION = (
    "Hand a task to another session and keep working — DETACHED fire-and-forget "
    "that does NOT hold your turn open (unlike ask/mssg, which block). The backend "
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

ENSURE_NAMED_WORKER_DESCRIPTION = (
    "Idempotently get-or-create a NAMED singleton worker by `worker:<name>` + cwd and "
    "return its agent_session_id. If a worker with that name and cwd already exists it "
    "is reused (created=false); otherwise a new one is provisioned with the given "
    "orchestration_mode and optional provision_prompt (created=true). Use this — not "
    "create_worker — when you want a STABLE, REUSABLE named worker reachable from any "
    "session (e.g. a single global cross-project worker). After it returns, delegate "
    "work with ask/mssg/delegate_task. Available to all sessions."
)
