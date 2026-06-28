# Better Agent — project-specific rules

For repo orientation and architecture, see the `project-structure` skill at
`.agents/skills/project-structure/`. This file is for non-obvious project
rules an agent should follow when editing the codebase.

## Provider capability configs

When adding or changing agent capabilities for any provider, use Provider
Config Sync's unified capability form first, then apply that capability to the
other configured providers. Do not edit only one provider-native config when
the capability has equivalents in Claude, Codex, or Gemini.

In multi-agent work, lock files before editing them. As soon as the files to
touch are known, call `lock_ops` with `keys=["file_edit:<absolute-path>", ...]`
and a reasonable `timeout_seconds`; do not edit files whose locks were not
acquired. If more files become necessary later, acquire their locks before
writing. Release file locks immediately after the edit/test/commit phase no
longer needs them, and always release them before the final response when the
runtime is still available. Use short leases so interrupted turns or tool
errors do not block other agents for long; after any resume or interruption,
re-check current state and reacquire locks before continuing writes.

Never RESTART an already-running backend server or frontend dev server
without explicit approval from the user, for ANY provider (Claude, Codex,
Gemini, or any other). STARTING a server that is not running is fine and
does not need approval. This rule is cross-provider: it applies to every
provider's backend/dev-server process uniformly.

Keep functionality parity across all supported providers: Codex, Claude, and
Gemini. When adding or changing a provider-facing feature, implement the
equivalent behavior for every supported provider in the same turn, or stop and
ask if true parity is not possible.

For provider tools/MCPs, prefer the provider-native configuration path whenever
it works. Detect config drift between the provider conversation's start-time
tool/MCP set and the currently desired set; when a resumed conversation is
missing newly desired tools and the native resume path cannot refresh them, use
the provider's per-turn dynamic tool mechanism to supply only the missing
capability for that turn. Do not replace a working native integration with a
dynamic fallback.

Keep desktop app behavior 100% aligned across macOS and Windows. When adding or
changing desktop startup, setup, update, packaging, or distribution behavior,
apply the equivalent macOS and Windows behavior in the same turn, or stop and
ask if true parity is not possible.

Every encountered issue that involves a project we maintain in Better Agent
must be looked at by delegating it to a task.

When committing and pushing changes that affect Provider Config Sync integration,
update the provider-config-sync checkout in the same session before reporting the
work as complete.

Long desktop/mobile artifact builds do not need to block the final response.
Start them in the background, keep a log path, and tell the user exactly what is
still running and where outputs/logs will land.

Every turn that writes to files MUST commit and push its own work before
reporting completion. Never leave local work hanging between turns.

The final message of every turn MUST include a short TLDR covering the whole
turn, including any steering messages received during the turn.

## Security is the top priority (read this before writing any code)

Better Agent is a destructive tool in the wrong hands: it spawns
real CLI subprocesses, executes arbitrary commands, reads/writes the
user's filesystem, and persists state to disk. Treat security as the
overriding constraint on every change — above features, above
convenience, above effort. When security trades off against anything
else, security wins; if you can't keep both, STOP and ASK.

Concretely, on every change:

- **Threat-model first.** Before adding an endpoint, command path,
  file operation, or way to influence a subprocess, ask: who can
  reach this, and what is the worst thing they can do with it?
- **Never trust input.** Validate and constrain everything that
  crosses a trust boundary — REST bodies/params, WS frames, env
  vars, file paths, model/tool/cwd selectors, anything from a worker
  or fork. Reject unexpected shapes; do not coerce or guess.
- **No command/path injection.** Never build a shell command or a
  filesystem path by string-concatenating untrusted input. Pass
  args as a list, not a shell string. Confine all filesystem access
  to the intended roots (go through `paths.bc_home()`); reject
  traversal (`..`, absolute escapes, symlink escapes).
- **Least privilege.** Subprocesses, tools, and approvals get the
  minimum scope needed. Don't widen permissions, auto-approve, or
  add escape hatches for convenience.
- **No secret leakage.** Never log, persist, echo to the UI, or send
  to a subagent: tokens, keys, internal auth, or full command
  environments. Scrub before it leaves the process.
- **Fail closed.** On any ambiguity about whether an action is safe,
  deny/abort — never fall back to the permissive path. (This is
  stronger than the usual "no fallback" rule: here the default on
  doubt is explicitly to refuse.)
- **Security review every change.** When a diff touches subprocess
  spawning, command execution, file I/O, auth, approvals, network
  surface, or anything that handles untrusted input, treat a
  security pass as part of the work — not optional. If a change
  expands attack surface and you can't fully reason about it, STOP
  and ASK.

## State ownership rule (read this before adding any frontend state)

**The backend is the single source of truth. The frontend only reflects
backend state — plus a small, well-defined sliver of optimistic state for
the gap between user action and backend acknowledgement.**

Concretely:

1. **Persistent state lives on the backend.** Sessions, workers, forks,
   approvals, projects, model selection, orchestration mode, cwd, inline
   tags, rearranger state — all of these are owned by the backend
   (disk-persisted via `session_store`, `worker_store`,
   `pending_approvals`, `project_store`, `config_store`). The frontend
   never holds a separate copy that can drift.

2. **The frontend reflects backend state via two channels:**
   - **Pull** — REST fetches (`GET /api/...`) on mount + refetch when
     the backend signals a change. Treat REST as a snapshot.
   - **Push** — WebSocket events (`worker_*`, `manager_*`,
     `worker_creation_*`, `user_message_persisted`,
     `rearranger_*`, etc.) for live deltas. Whenever the backend
     mutates something the frontend cares about, it MUST emit a WS
     event so any open client can react without polling.

3. **The ONLY frontend state allowed without a backend authority** is
   the optimistic ack-bridge and the durable offline-action backlog:
   the slice of time between "user performed an action" and "backend
   confirms it received the action". Examples:
   - `pendingMessages` — local user-prompt entry shown immediately,
     cleared the moment `user_message_persisted` arrives.
   - Offline actions persisted in frontend `localStorage` until they
     can be synced and acknowledged by the backend.
   - In-flight modal form values, button "submitting…" disabled flags,
     scroll position, expand/collapse toggles, viewing-file path —
     pure transient UI that doesn't represent persistent application
     state.

4. **Selectors that drive backend behavior** (model, cwd,
   orchestration_mode) are persistent state and therefore live on the
   session record. Frontend changes must round-trip to the backend
   immediately (debounced PATCH or explicit save), not be queued for
   "next send".

5. **Anti-patterns that violate this rule:**
   - Holding a list of workers/sessions/projects in frontend state and
     only refreshing via a manual button. Stale data is wrong data.
   - Computing derived state (counts, divergence flags, timestamps) in
     the frontend from cached snapshots. Compute server-side, push via
     WS.
   - localStorage shadowing of state that already lives on the backend.
     localStorage is fine for pure UI prefs (panel widths, theme) and
     the unacknowledged offline-action backlog, not for acknowledged
     backend state.
   - "Optimistically remove on click, refetch on next render" patterns
     that hide the lack of a server push. If the user can resolve a
     thing in tab A, tab B must learn about it via WS — don't rely on
     tab B's stale local state.

6. **When you add a new feature, ask in this order:**
   1. Where does the persistent state live on disk? (Add it to the
      relevant `*_store.py` or create a new one.)
   2. How does the frontend pull a snapshot? (REST endpoint.)
   3. How does the frontend learn about changes? (WS event type.)
   4. Only then: what optimistic UI bridges the user action → ack
      window?

   If you skip steps 1-3 and put state in a `useState` "for now",
   you're introducing drift. Don't.

## Offline-first usability

**Maximize useful work while the frontend cannot reach the backend.**
User intent must not be lost merely because the backend or internet is
temporarily unavailable.

- Maintain a durable frontend action backlog in `localStorage` for
  actions the user performs while offline. This includes creating a
  new session with its initial prompt and sending prompts to existing
  sessions.
- Render backlogged actions immediately as clearly pending/offline so
  the user can continue capturing work.
- On reconnect, sync the backlog to the backend and let the normal
  backend-owned creation, persistence, validation, event, and execution
  paths handle every action. The frontend backlog is a transport queue,
  never a second source of truth.
- Remove an action from the backlog only after an explicit backend
  acknowledgement. Sync must be idempotent and preserve user action
  order so reconnects, reloads, duplicate attempts, and partial syncs
  cannot lose or duplicate work.
- After acknowledgement, replace any temporary frontend identifiers and
  projections with the authoritative backend state.
- Prefer extending this backlog to other safe, meaningful user actions
  when doing so improves offline usability. Do not queue actions whose
  delayed execution would be unsafe or surprising without making that
  behavior explicit to the user.

## Event-driven projection is the preferred decoupling pattern

When one subsystem owns durable state, other subsystems should not
mutate that state directly. They should publish a domain event/fact on
the backend event bus, and the owning subsystem should subscribe and
update its own projection/read model.

Use this pattern by default:

- **Writer owns the log.** A journal/event writer appends durable facts
  to its log and emits a written/failed acknowledgement.
- **State owner owns denormalized state.** If `session_manager` owns
  `session.json`, then `session_manager` updates `session.json` from a
  bus event/projection handler; another subsystem should not call a
  session-manager mutator just to keep a projection fresh.
- **Readers own read projections.** Read-side helpers may build
  frontend-facing projections from logs and caches, but they do not
  mutate owner state.
- **Prefer event-driven projection / read-model updates over direct
  cross-owner mutation.** This is the local form of CQRS/event-driven
  architecture: emit facts, let the owner project them. Rule of thumb:
  publish FACTS (what happened — "a subscriber attached", "a sid was
  discovered"), never COMMANDS (what to do — "start tailing X"). The
  owning subsystem decides what to do with the fact, at its own
  discretion. (`native_files_manager` is the reference example: other
  subsystems fire `native_files.demand` / `native_files.fork_target`
  facts; the manager alone decides which tailers to open/close.)

Exception: a narrow direct call is acceptable only inside the owning
module/boundary or during a deliberately temporary migration step that
is documented near the call site.

## Session event ingestion — three scenarios MUST converge

There are exactly three ingestion scenarios for session events, and
the persisted **render-tree state** they produce MUST be identical
(the WS frame sequence is NOT identical by design — see below).

1. **Live (frontend + backend online)** — claude subprocess writes
   its jsonl AND the SDK callback in `runner.py` fires events back
   to the orchestrator → `save_ws_callback` (in
   `backend/orchestrator.py`) → `_apply_event_to_assistant_msg` →
   `strategy.apply_event(... live=True)` (in `backend/orchs/base.py`).
   This is the only producer while a turn is in flight.

2. **Frontend offline, backend online** — IDENTICAL backend path to
   scenario 1. The only difference: `session_manager._fire` →
   `SessionWSBroadcaster.on_change` fans out to zero subscribers,
   and `BetterAgentJsonlTailer` drops `events.jsonl` frames into an
   empty `_subscribers` dict. On reconnect, two distinct channels
   rehydrate:
   - `messages_replay` (in `backend/main.py`) — built from
     `session_manager.get_ref(sid)` + the in-flight assistant msg
     ref. This is the render-tree snapshot.
   - `_Subscriber.catch_up_to(cursor)` (in `backend/jsonl_tailer.py`)
     — replays typed event frames from `events.jsonl` past the
     subscriber's watermark. This is the event stream.

3. **Restore (both were offline, backend just restarted)** — the
   detached runner kept appending events AND the provider's
   underlying CLI kept appending its own stream (claude session
   jsonl for Claude, `session_events.jsonl` for Gemini). On startup,
   `recover_all_in_flight` (in `backend/provider.py`) dispatches
   per-provider, scans `~/.better-claude/runs/` for dirs without
   `reconciled.marker`, and classifies as already_complete /
   dead_orphan / live_no_rehook. Then `integrate_recovered_runs` →
   `_integrate_one` → `_replay_and_apply` (all in
   `backend/run_recovery.py`) re-reads the provider's stream and
   replays each event through `strategy.apply_event(... live=True)`.
   UUID + sha256(data) dedup in `event_ingester.ingest` prevents
   duplicate `events.jsonl` entries. Gemini's `runner_gemini.py`
   pre-normalizes to Claude-shaped events on the way to
   `session_events.jsonl`, so the replay funnel is identical from
   `apply_event` onward.

**The convergence invariant — what MUST hold:**

- After all three scenarios apply the SAME completed event sequence,
  the persisted render tree (the `messages` / `manager.events` /
  worker-panel events on disk under `~/.better-claude/sessions/`) is
  byte-identical modulo timestamps and append order across
  concurrent producers.
- `events.jsonl` for the root is byte-identical modulo timestamps
  and append order (dedup makes the live and replay writers
  idempotent against each other for the same `(uuid, data)` pair —
  streaming updates with the same uuid but mutated data DO append a
  new row).
- Eventual, not unconditional: if the runner or the underlying CLI
  crashes mid-line and only one of the two writers got the event to
  disk, the two writers re-converge on the next backend startup
  once `_reconcile_msg_events_from_jsonl` runs. A divergence that
  survives a clean restart is a bug.

**What is NOT in the invariant:**

- The WS frame sequence is NOT identical across scenarios. Live
  emits `turn_start` / `manager_event` / `turn_complete` /
  `turn_stopped` framing via direct
  `ws_callback({...})` calls in the orchestrator. Recovery emits
  none of those — only the rehydrated render tree and `events.jsonl`
  matter for a restart. Scenarios 1, 2, 3 produce IDENTICAL
  post-load state but DIFFERENT framing during the load itself.
- Metadata events (`ai-title`, `file-history-snapshot`) intentionally
  bypass `msg.events` — they live only in `events.jsonl`. This is
  symmetric across all three scenarios; the asymmetry is between
  metadata vs. rendered events, not between scenarios.

**To uphold the invariant:**

- **`OrchestrationStrategy.apply_event` in `backend/orchs/base.py`
  is the single funnel for render-tree mutation.** Anything that
  touches `msg.events`, `msg.manager.events`, or worker panel events
  goes through it. Worker panel events from delegations route
  through the parent turn's `save_ws_callback` → `apply_event` (see
  `orchs/manager/_delegation.py`).
- **`OrchestrationStrategy.ingest_orphan` is the SRP-paired write
  path for events whose source is the primary CLI session jsonl but
  no streaming assistant_msg owns them yet** — e.g. the primary
  `OwnedClaudeJsonlTailer` firing after the orchestrator finalized
  the turn. Calls `event_ingester.ingest(... msg_id=None)`; reuses
  the ingester's built-in `mark_reconcile_dirty` so a later read
  seq-brackets the orphan onto the right msg. NO render-tree
  mutation. Distinct from `apply_event` because `apply_event`'s
  contract is render-tree mutation; the orphan path shares only the
  events.jsonl tail.
- **For events sourced from the PRIMARY agent's CLI session jsonl
  (manager / native / supervisor `*_agent_session_id`), the single
  writer is `apply_event` (streaming msg present) or `ingest_orphan`
  (no streaming msg).** Live ingest from the SDK callback, the
  primary `OwnedClaudeJsonlTailer`'s tail-side fallback, and
  crash-recovery replay all funnel through this single pair. The
  tailer's `_dispatch` re-checks `is_primary` and re-fetches the
  streaming msg inside `session_manager.batch(...)` to close the
  gate-check ↔ apply race; `ingest_orphan` runs OUTSIDE the batch
  to preserve the documented `event_ingester → session_manager`
  lock order.
- **Worker-fork tailers** (the per-`fork_agent_sid` tailers reconciled
  by `native_files_manager` for every persisted worker panel, fed by
  `native_files.fork_target` facts emitted in `_delegation.py`) are a
  legitimate **secondary writer** to `events.jsonl` with
  `source="claude_tailer"`. They
  cannot funnel through `apply_event` because they were constructed
  with `app_session_id=PARENT_app_session_id` — routing them through
  `apply_event(msg=parent_streaming_msg)` would graft worker raw
  SDK lines onto the parent manager's events list. The fork's
  events are owned by the worker panel, not the parent msg. The
  direct ingest is a crash-window backup; the worker's own
  `apply_event` (driven by the delegation turn) is the primary
  producer for fork events; dedup at `event_ingester` (uid +
  sha256(data)) collapses the overlap into a no-op in steady state.
  (A pre-existing rare-case reconcile-pollution issue with this
  secondary writer's `msg_id` stamping is filed as a separate
  follow-up; it is independent of this funnel. A deeper fix would
  route `worker_event` correctly inside `apply_event` so the
  worker-fork tailer can also funnel through the single path.)
- **The `live` flag inside `apply_event` gates exactly four
  side-effects:** the `file_ref_resolver.rewrite_event_data` call,
  the `event_ingester.ingest` write to `events.jsonl`, the
  `_fire_user_msg_received_if_pending` lifecycle emit, and the
  attention-marker detection (`file_ref_resolver.detect_markers` on
  the RAW assistant text → `session_manager.set_marker`). The marker
  scan MUST run on raw text captured BEFORE `rewrite_event_data`
  strips the `<TAG>` wrapper out of `msg.events`; the set is
  change-gated so streaming deltas of one turn broadcast at most once.
  The render-tree mutation (dedup + append/replace on `msg.events`) is
  identical in both branches.
- **`live=True`** when the source is raw provider data (live
  ingest, crash-recovery replay from the provider's CLI stream).
  Safe during recovery because `event_ingester.ingest` is
  idempotent on `(uuid, sha256(data))`. The
  `_fire_user_msg_received_if_pending` emit during recovery is a
  no-op in practice (no pending user-msg ack to fire) — rely on
  that semantic, not on the flag.
- **`live=False`** when the source IS `events.jsonl` (reconcile
  path). Entries are already file-ref-rewritten by
  `event_ingester._emit`, so skipping rewrite is correct;
  re-ingesting would duplicate, so skipping ingest is correct. (On
  the live path the rewrite happens twice — once in `apply_event`,
  once in `_emit` — idempotent, expected.)
- **Dedup semantics differ by surface — keep them coherent:**
  `event_ingester` dedupes by `uid:sha256(data)`, so an event with
  the same UUID but mutated data (Gemini streaming, in-place
  updates) APPENDS a new row. `apply_event` dedupes `msg.events` by
  UUID alone and REPLACES the existing entry on data change.
  Multiple disk rows, one render-tree row. Don't add a third dedup
  with different rules.
- **Single WS broadcast path for state mutations.**
  `session_manager._fire` → `SessionWSBroadcaster.on_change` (in
  `backend/session_ws_broadcaster.py`) is the ONLY way state
  mutations become WS frames. Direct `ws_callback(...)` calls in
  the orchestrator emit *framing* events (`turn_start`,
  `turn_complete`, etc.) — they do NOT mutate state and are not
  part of the convergence invariant.
- **Post-turn helpers (e.g. `session_manager.snapshot_workers`
  called from `orchestrator.py` outside `apply_event`) overwrite
  the same fields `apply_event` already maintained.** They're
  allowed because they're idempotent finalizers, not new state. New
  post-turn helpers MUST follow the same idempotent-overwrite rule,
  or move inside `apply_event`.

If you find yourself writing a "fast path" for restore that skips
normalization, file-ref rewrite, lifecycle emits, store writes, or
trace collection: stop. Gate the side-effect inside the shared
funnel with an explicit flag (like the existing `live` flag); don't
fork the ingestion code.

Three integration tests lock this invariant — don't disable any:
- `backend/scripts/test_apply_event_unified.py` — locks the
  `live=True` vs. `live=False` semantics inside `apply_event` and
  the reconcile path.
- `backend/scripts/test_recovery_render_consistency.py` — locks
  scenario 3's render-tree convergence against scenario 1.
- `backend/scripts/test_tailer_routes_through_apply_event.py` —
  locks the primary-agent tailer funneling through
  `apply_event` / `ingest_orphan`, AND the worker-fork tailer
  keeping the legacy direct ingest (regression-locks the gate that
  prevents worker raw lines from polluting the parent manager's
  events list).

## Never leave anything opened (project-specific)

The Better Agent backend spawns durable processes and state that outlive a single turn: detached runners, worker/fork sessions, jsonl tailers, WS subscribers, run-recovery handles, background dev servers, and git worktrees. Never leave any of these dangling after your turn.

- Close/clean up every resource you opened: stop spawned runners/sessions/workers, tear down worktrees you created, kill background servers you launched, release locks, remove temp files.
- The ONLY exception is a resource the user explicitly asked to keep alive. In that case, the FINAL turn of the task MUST explicitly list every item that is still open — process names/PIDs, session/worker IDs, worktree paths, ports. "At minimum, always show in the last turn what is still open."

## Todo management is mandatory (project-specific)

Every unit of work in this repo — even a one-line config edit or a single test run — must be represented on the todo list (TaskCreate / TaskUpdate) before it is performed: create, set in_progress, do, mark completed. Never perform any work that is not represented as a todo item. No task is too small to be tracked.

## Other project rules

- Tests live in `backend/scripts/integration_test*.py` and run real
  claude CLI subprocesses. They're slow but they catch the wiring
  bugs unit tests miss. Don't add unit-style mocks.
- Schema migrations are NOT supported. Bump the version, raise on
  unexpected shape, document "wipe X to start fresh." See
  `worker_store.py`.

## State directory isolation — `BETTER_AGENT_HOME`

**Never write code, scripts, or shell commands that touch
`~/.better-claude/` or `~/.better-agent/` directly.** Always go
through `paths.bc_home()` (in `backend/paths.py`), which honors
`BETTER_AGENT_HOME`, falls back to legacy `BETTER_CLAUDE_HOME`, and
defaults to `~/.better-claude` with a local `~/.better-agent` alias
when possible.

This rule exists because tests, dev scripts, and one-off cleanup
commands have repeatedly clobbered the developer's real session
state when sharing the default home. Every persistence module
(`session_store`, `worker_store`, `pending_approvals`, `project_store`,
`provider_bridge.RUNS_ROOT`, `trace_collector`,
`rearranger_state`, `orchestrator._internal_token_path`) goes
through `bc_home()`.

When writing tests:
1. Set `os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(...)`
   BEFORE importing any backend module. Legacy `BETTER_CLAUDE_HOME`
   remains supported for older tests.
2. `rmtree(bc_home)` on exit — safe because it's a tempdir.
3. Never `rm -rf ~/.better-claude/anything` or
   `rm -rf ~/.better-agent/anything` from a script. If you want a
   clean test, set `BETTER_AGENT_HOME` to a fresh tempdir and the
   entire env is fresh by construction.

When writing dev/admin scripts: same rule. If a script needs the
real home, it should accept an explicit path argument, not assume
either default state directory.

## Repository Rules

- The public `better-agent` repository must work with GitHub.
- The private `better-agent-private` repository must work with GitLab.
