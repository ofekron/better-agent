# SPEC — Session Bridge MCP (cross-session search + delegate)

Status: Implemented. Code in `backend/session_bridge.py`, `backend/runner.py` (tool builders), `backend/main.py` (internal endpoints).

A new in-process SDK MCP server exposed to **every** BC session (native
and manager), independent of the manager `delegate` tool. It gives any
running session two capabilities:

1. **Discover** other BC sessions by content + metadata (`search_sessions`).
2. **Delegate** a prompt to a chosen session and get the result back
   (`delegate_to_session`) — using the lightweight native/`fork_and_send`
   path, with optional human gating via the Ask session picker.

The caller searches, inspects results (score + fit reason), decides
which session fits, then delegates. The two are deliberately separate
tools so the caller keeps full freedom over target selection.

---

## §1. Hierarchies

### 1.1 Session Search (one base, escalating capability)

- **1.0 Base — metadata scoring.** Reuse `score_sessions` (the Ask
  singleton's scorer): ranks on `name` (3×), `first_user_prompt` (1×),
  `project_name` (0.5×). Cheap, no content read.
- **1.1 Extension — content grep, MODEL-CALLABLE.** The ephemeral search
  agent holds a `grep_sessions` MCP tool (wrapping
  `session_store.grep_sessions`, restricted to listable sessions) and may
  call it **N times** with refined queries to confirm a session really
  discusses the topic — the model drives the escalation, not the server.
  Reference pattern: the global `~/.claude` `grep-all-sessions.sh` helper.
  No new grep engine — wrap the existing one.

### 1.2 Delegation Run Mode

- **2.0 Base — run a prompt against a target session, return the final
  result.** Built on the native/`fork_and_send` path, NOT the manager
  approval-Future dance.
- **2.1 `fork`** — run on a copy; original session untouched.
- **2.2 `continue`** — append a real turn to the target in place.

### 1.3 Approval

- **3.0 Base — gate cross-session writes.**
- **3.1 `require`** — present the Ask session picker; the user
  chooses/confirms the target, then delegate like the Ask flow. Always
  allowed.
- **3.2 `auto`** — no picker. Allowed ONLY when the config flag
  (INV-4) is ON **and** `run_mode == "fork"`. Otherwise routed to the
  picker / rejected (fail closed).

---

## §2. Base abstractions & extensions (functional requirements)

### Tool A — `search_sessions`

- FR-A1. Input `{query: string, limit?: int}`.
- FR-A2. Returns a structured, agreed result so the **caller** decides.
  Per candidate: `{id, name, cwd, first_user_prompt, score, fit_reason,
  snippet?}`. `fit_reason` is produced by a **nested AI pass** (option
  (b)): the search tool internally runs a short model turn that, given
  the metadata-scored + grep candidates, writes a per-candidate
  explanation of WHY it fits the query. `score` is the metadata rank.
  The caller uses `fit_reason` + `score` to choose a target itself.
- FR-A3. Pure read. No mutation, no side effects, no inline-picker
  commit (search never stamps `ask_result`).
- FR-A4. Implemented as the existing AI-search session logic
  (`session_search.py` / `score_sessions`), but **un-scoped** so any
  session can invoke it — not gated on `app_session_id == "ask-singleton"`.
- FR-A5. The search runs as a **dedicated ephemeral agent** (option (a)):
  `session_bridge.search()` creates a hidden, throwaway native BC session
  (`sbsearch-<rand>` id, working_mode `sb_search` → hidden from sidebar &
  index, cwd under `ba_home()`), runs ONE model turn, reads the agent's
  committed candidates off its assistant message, then **deletes** the
  session. Per-call session ⇒ naturally concurrent (NOT the shared Ask
  singleton). Uses the `_run_search`-style in-process turn driver
  (register_ws watermark + `lifecycle_msg_id` Future + submit_prompt).
- FR-A6. The ephemeral agent is handed a `bridge-search` MCP server with
  three **model-callable** tools (runner.py, gated on the `sbsearch-` id
  prefix), each POSTing to a token-gated internal endpoint:
  - `score_sessions` — metadata relevance (`session_search.score_sessions`).
  - `grep_sessions` — full-content grep (`session_bridge.grep_listable` →
    `session_store.grep_sessions`, restricted to listable sessions). The
    agent MAY call it **N times** with refined queries to confirm a
    session truly discusses the topic.
  - `propose_candidates` — commits `[{id, fit_reason}]`; the endpoint
    validates ids to listable-only (`validate_candidates`, fail closed)
    and stamps them on the ephemeral session's in-flight assistant message
    (`set_msg_ask_result` → `{sb_candidates}`).
  `search()` then enriches the agent's picks with display metadata + a
  best-effort metadata `score`, and returns the top `limit`.

### Tool B — `delegate_to_session`

- FR-B1. Input `{session_id, prompt, run_mode, approval}`.
  - `run_mode`: `"fork" | "continue"`
  - `approval`: `"auto" | "require"`
- FR-B2. Resolves `session_id`; rejects unknown / non-existent ids
  (fail closed, no guess).
- FR-B3. `fork` → fork the target, enqueue prompt on the fork (reuse
  `fork_and_send`). `continue` → enqueue a real turn on the target.
- FR-B4. `approval:"require"` (and any auto-disallowed case) → present
  the **Ask-style session picker** (reuse the `propose_sessions` /
  inline-picker UI) so the USER chooses/confirms the target session,
  then delegate exactly like the Ask flow. User cancel → abort, no turn.
  The picker IS the approval surface (no bespoke approve/deny modal).
- FR-B5. `approval:"auto"` → run immediately without a picker IFF the
  config flag is ON **and** `run_mode == "fork"`. `continue` is stricter
  (INV-8): it always goes through the picker even when the flag is ON.
  Auto with flag OFF → routed to the picker (fail closed).
- FR-B6. Returns a single agreed format: the **final** assistant message
  of the delegated turn plus ids — `{session_id, run_mode, final_message,
  turn_id}`. No result-mode param.
- FR-B7. Runs on the native path — no manager system prompt, no
  `delegate` MCP tool injected into the target.

### Tool C — `recall_history` (per-session semantic recall)

- FR-C1. Before a `continue` delegation runs, `session_bridge.delegate`
  builds a per-session embedding index of the target chain's prior
  transcript (`session_recall.build_index`). Best-effort: an index
  failure NEVER blocks the delegation. (Fork: TBD — pending scope
  decision; a fork's child inherits the parent history, so recall is
  equally applicable.)
- FR-C2. The delegated (and any user-facing) turn gets a `recall_history
  {query, k}` MCP tool → `/api/internal/session-bridge/recall` →
  `session_recall.recall(app_session_id, query, k)`. Returns the top-k
  cosine-similar transcript chunks `{role, message_index, score, text}`.
  Empty when no index was built (recall is opt-in per delegation).
- FR-C3. Embeddings use the backend's existing local `model2vec`
  embedder (`project_match.embedding`) — pure-numpy cosine, no new dep,
  no daemon. NOT cocoindex (a standalone daemon, not library-accessible).
- FR-C4. Per-session only — indexing ALL sessions is too large. The index
  is in-memory, cached by `(sid, message_count)`, rebuilt only when the
  session grows.

### Registration

- FR-R1. New `create_sdk_mcp_server("session-bridge")` built in
  `runner.py`, added to EVERY session's MCP set (native + manager),
  not conditioned on mode.
- FR-R2. Both tools POST to new internal endpoints (mirroring
  `/api/internal/delegate`, `/api/internal/ask-search`): e.g.
  `/api/internal/session-bridge/search` and `/.../delegate`.

---

## §3. User-facing flows

- **Search:** When I'm in any session and need prior work, I want to ask
  "find sessions about X" and get ranked candidates with a fit reason +
  score, so I can pick the right one myself.
  - Given any running BC session, When I call `search_sessions{query}`,
    Then I get ranked candidates with `score` + `fit_reason`; And if
    metadata is thin the AI has already grepped content to improve them.
- **Delegate (gated):** When I've picked a session and auto is off, I
  want the Ask picker to confirm the target, then run my prompt on it.
  - Given a valid `session_id` And auto OFF (or `run_mode=continue`),
    When I call `delegate_to_session`, Then the Ask picker is shown; And
    on user confirm the turn runs and I get the final message; And on
    cancel nothing runs.
- **Delegate (auto):** Given auto flag ON And `run_mode=fork`, When I
  call `delegate_to_session`, Then it runs immediately with no picker and
  returns the final message.

---

## §4. Invariants

- **INV-1. Independent of manager delegate.** Session Bridge tools never
  route through `coordinator.run_delegation` / the manager approval
  dance. They use the native/`fork_and_send` path. The manager
  `delegate` tool is unchanged.
- **INV-2. Search is pure read of the user's sessions.** It never mutates
  USER session state, never stamps `ask_result` on a user turn, never
  broadcasts a picker. The ONLY state it creates is the hidden ephemeral
  `sbsearch-` session, which it deletes in a `finally` after the turn — a
  disposable internal scratch session, not user-visible state. (Egress
  note: the ephemeral agent runs a real model turn, so grep/score results
  — including session names and transcript snippets surfaced by grep — go
  to the active provider. Same provider already running the user's turns;
  within the existing trust boundary, but a wider content surface than the
  metadata-only path it replaced. Acceptable; flagged for awareness.)
- **INV-3. Fail closed on selection & approval.** Unknown `session_id`,
  unexpected param shape, or `auto` while disallowed → picker or reject,
  never the silent permissive path.
- **INV-3a. Caller must be a live in-flight turn — on EVERY path.**
  `delegate` re-checks server-side (`turn_manager.get_in_flight_assistant_msg`)
  that `caller_sid` has an in-flight assistant message before doing
  anything, auto path included. This is defense-in-depth behind the
  runner gate (only user-facing turns get the tool) so a stray
  internal-token holder can't drive a delegation for a session with no
  live turn, and so `auto` is never weaker than `require`. (Residual,
  matching the existing manager-delegate trust model: a worker turn does
  hold the shared internal token and has its own in-flight msg, so this
  check does not by itself distinguish worker vs. user turns — the
  runner-side `open_file_panel_enabled` gate is the primary exclusion.)
- **INV-3b. `continue` refuses a busy target.** Because the
  `user_message_done` frame carries no assistant-msg id, the post-turn
  result is correlated only by message order; to keep that sound (and to
  avoid blocking behind another turn for up to the 24h budget),
  `continue` rejects a target that already has an in-flight turn
  (`target_busy`). `fork` is immune — its child is a private, freshly
  created session with no concurrent writer.
- **INV-4. `auto` is gated by one config flag, default OFF.** A single
  authoritative setting in the config store (e.g.
  `cross_session_delegate_auto_enabled`, default `false`) is the ONLY
  thing that permits no-picker `auto`. Single source of truth — no
  per-session shadow copy.
- **INV-5. Filesystem confinement.** All session-file access (grep,
  fork) goes through `paths.ba_home()` / `session_store`; no raw
  `~/.better-claude` paths, no traversal.
- **INV-6. Backend is source of truth.** The tools read/write only
  backend-owned session state; results returned to the caller are
  projections, not new authoritative state.
- **INV-7. Single grep engine.** Content grep reuses
  `session_store.grep_sessions`. No second grep implementation.
- **INV-8. `continue` is stricter than `fork`.** A `fork` is a
  non-destructive copy and MAY auto-run when the flag is ON. A
  `continue` mutates an existing session in place and ALWAYS requires
  the picker confirmation, regardless of the flag.
- **INV-9. Reused picker is the approval surface.** The human-gate for
  delegation is the existing Ask inline session picker, not a new
  bespoke approval modal. One picker implementation, two callers (Ask
  search, session-bridge delegate).
- **INV-10. Recall is self-scoped.** `recall_history` searches ONLY the
  caller's OWN session (`app_session_id`) index — never another
  session's. The endpoint keys the lookup by `app_session_id`; there is
  no parameter to query a different session's transcript. No new
  cross-session read surface.

---

## §5. NFRs

- `search_sessions` (option a) spawns a real CLI subprocess per call
  (cold-spawn latency, seconds) and runs ≥1 model turn — the cost of
  giving the agent iterative, model-driven grep. Acceptable for an
  interactive discovery tool; the ephemeral session is deleted after.
  90s turn timeout (`_SEARCH_TURN_TIMEOUT`).

- Grep is brute-force over JSON files (same as today's
  `/api/sessions/search-content`); acceptable for current scale. If it
  becomes hot, add a cache projection — never a second source of truth.
- `delegate_to_session` blocks the caller's tool call until the target
  turn completes (like manager `delegate`); use a long timeout for the
  picker-wait case.

---

## §6. ADRs

- **ADR-1. Reuse `score_sessions` + `grep_sessions` rather than a new
  search.** Un-scope the scorer; wrap grep as an internal tool for the
  search model. One search brain, two data sources, no new index.
- **ADR-2. Two tools, not one combined.** Keep `search_sessions` and
  `delegate_to_session` separate so the caller decides the target.
- **ADR-3. Gate `auto`, don't remove it.** Keep `auto` for
  experimentation but behind a default-OFF flag; default is the picker.
- **ADR-4. Reuse the Ask picker as the approval surface.** Avoids a
  second picker/approval UI; the human confirms the target there.

---

## §6a. Known residuals / accepted risks

- **R1. `/resolve` is browser-facing**, gated by the SAME global
  `auth_gate` middleware (main.py: `CORS → SessionMiddleware →
  auth_gate → ingest`) as every other `/api/...` route — e.g.
  `ask-choice`. It is therefore NOT unauthenticated; it must NOT take
  `X-Internal-Token` (that token is for runner→backend loopback; the
  browser has none). Target injection is blocked independently — only an
  id in the delegation's `proposed_ids` (the single confirmed target) is
  accepted, else None=cancel; `delegation_id` is 48-bit random. Residual:
  across multiple authed clients of the SAME user, any client could
  confirm/cancel another's pending delegation. Acceptable for the
  single-user model; bind resolve to the caller session if BC ever needs
  per-client isolation.
- **R2. Stale picker footer in other tabs.** When a delegation resolves
  or times out, the `delegate_approval` footer in a second tab is not
  actively cleared (no resolved/expired WS event yet). The
  resolve endpoint 409s a stale click, so it's safe — just visually
  stale. Follow-up: emit a `message_ask_result_changed` clearing event
  on resolve/expire.

## §7. Out of scope

- No change to the manager `delegate` tool.
- No NEW picker UI — reuse the existing Ask inline session picker.

## §8. Open questions / evolution notes

- All prior open questions RESOLVED:
  - Search result = agreed structured format with `fit_reason` + `score`
    (FR-A2); caller decides.
  - `continue` stricter than `fork` (INV-8): always picker-gated.
  - Auto flag surfaced in the **settings UI** (OQ-3): backend config
    store is the single source of truth, settings UI reads/writes it.
  - Delegate returns **final** message only (FR-B6); no result-mode param.
- Picker decoupling RESOLVED. `SessionPicker` (frontend/src/AskPanel.tsx)
  is already presentational/context-agnostic. Coupling lives only in
  App.tsx: the `isAskView` render guard (3463), `handleAskChoose` (2865),
  `handleAskCreateNew` (2929). Reuse plan: discriminate `ask_result` with
  `purpose: "ask" | "delegate_approval"`; render the footer on a pending
  delegate-approval for any session's turn; pass a delegate `onChoose`
  that POSTs to the session-bridge approval endpoint (resolves the Future).
  Same component, new condition + handler. INV-9 holds.
