# Better Agent — Specification

> **Synthesis state** — `last_user_prompt_ts: 2026-05-13T22:44:34.538Z` · `source: claude/25e70a99` · `corpus_size: 955` unique prompts. Last delta (11 prompts past prior marker): ~4 noise (one-word `approve`, "claude provider doesn't work" bug ping, "leave it for now" meta-directive, and the full auto-injected file-edit meta-prompt body — OQ-16 class), ~7 legitimate, folded into §4.1, §10. Re-synthesis should consume prompts in `user_prompts.jsonl` with `ts > last_user_prompt_ts` and update this marker. Update protocol: scan the delta, fold legitimate new requirements into the relevant §4/§5/§6/§7/§8 sections, list noteworthy findings, then advance the marker to the highest `ts` actually read (not the file's tail, in case the tail was filtered as noise).

## 1. Purpose & Scope

Better Agent (current direction: "Better Agent") is a multi-provider web UI currently supports Claude Code and Gemini CLI sessions. It layers manager/worker delegation, supervisor review, file-centric edit modes (prompt engineering, settings/harness editing, generic file edit, plan refinement), session forking, and trace inspection on top of native CLI sessions. Backend: Python + FastAPI; frontend: React + TypeScript. All persistent state lives on the backend under `BETTER_AGENT_HOME` or legacy `BETTER_CLAUDE_HOME` (default owner `~/.better-claude/`, local alias `~/.better-agent/` when possible); the frontend reflects backend state plus a thin optimistic ack-bridge. Multi-provider/multi-mode reuse comes from small base classes with concrete subclasses; no per-mode `if` chains in shared shell code. A naming sweep (`claude_*` → `agent_*` on shared bases) is in progress.

## 2. Actors & External Interfaces

- **End user** — drives sessions via web UI or in-process CLI driver.
- **Native CLI subprocesses** — `claude`, `gemini`. Detached runners survive backend restart and re-parent to launchd.
- **MCP servers** — in-process `delegate` SDK MCP tool (manager mode); external MCP servers for non-BC sessions (e.g. `ccc`).
- **Providers** — Claude (subscription OAuth, or api_key with optional `base_url` like z.ai and optional `CLAUDE_CONFIG_DIR`); Gemini (subscription). Selected per session; secrets in macOS Keychain.
- **Native session files** — provider-specific JSONLs under the provider's `config_dir` (e.g. `~/.claude/projects/...`, `~/.claude-zai/projects/...`) plus `subagents/agent-*.jsonl`.
- **WebSocket `/ws/chat`** — live push channel. **REST `/api/*`** — snapshot endpoints.

---

## 3. Hierarchies at a glance

```
FILE MODE (base)
├── 1 prompt-engineering        (refines a draft user prompt)
├── 2 generic file-edit         (any project file)
├── 3 project-settings AI-Edit  (CLAUDE.md, settings.json, skills, hooks, keybindings)
├── 4 project-harness-file edit (skill/hook files)
└── 5 plan mode (proposed)      (refines a plan document)

ORCHESTRATION MODE (base: OrchestrationStrategy ABC, FE + BE)
├── 1 Native
├── 2 Manager
└── 3 Supervisor                (SIBLING of Manager, not subclass)

PROVIDER (base: build_env, config_dir, model fetch, Keychain creds)
├── 1 ClaudeProvider   (subscription | api_key — api_key may set base_url + CLAUDE_CONFIG_DIR)
├── 2 GeminiProvider
└── 3 generic Agent (future)

SESSION (base: id, cwd, model, provider_id, orch_mode, forks, persistence)
├── 1 regular user session
├── 2 supervisor_worker          (paired internal)
├── 3 engineering_worker         (ephemeral, hidden from sidebar)
├── 4 file_edit_worker           (ephemeral, hidden from sidebar)
└── 5 plan_worker (proposed)     (ephemeral, hidden from sidebar)

WORKER (per-cwd registry; per-(caller, worker) fork)
├── 1 user-marked existing session
└── 2 freshly created (with approval)

TAILER (JSONL ingestor)
├── 1 ClaudeTailer
└── 2 GeminiTailer

INGESTION PATH (singleton; is_replay flag gates side-effects;
                same projection feeds REST + WS replay/delta)

EVENT/MESSAGE (base: uuid, parent_uuid, msg_id, ts, entity_id, kind)
├── 1 user_prompt        2 assistant_text     3 tool_call
├── 4 tool_result        5 subagent_event     6 supervisor_event
├── 7 worker_event       8 rearranger_update  9 queue_op

APPROVAL (disk-backed, fcntl-locked, idempotent across tabs)
└── 1 fresh-worker-approval  (top-level only; nested = resume-only)
```

> FRs are keyed `FR-<HIER>.<level>.<n>`. `*.0.*` are base rules; `*.k.*` (k≥1) are deltas of the k-th subclass listed above.

---

## 4. Base abstractions & their extensions

### 4.1 File Mode

The unifying frame for "side-by-side chat + file viewer; iterate on a file via a paired ephemeral worker; on Send, finalize the file and dispose the worker."

#### 4.1.0 Base rules
- **FR-FILE.0.1** Entering File Mode swaps the main panel into chat (left) + file viewer with diff against baseline (right). The file viewer MUST be auto-opened (panel visible, file loaded, diff baseline computed) on entry — the user MUST NOT have to manually open or reveal it.
- **FR-FILE.0.2** Entry creates a paired **ephemeral worker session** dedicated to one target file; cwd matches parent (fork) or project root (fresh).
- **FR-FILE.0.3** Ephemeral workers MUST NOT appear in the sidebar.
- **FR-FILE.0.4** User MAY leave and return to an active File-Mode session without losing work.
- **FR-FILE.0.5** Text selection in the file viewer raises the same "Copy / Comment" popover as chat; file-anchored comments carry `file:line(:col)` pointers (not embedded selections).
- **FR-FILE.0.6** Where meaningful, the user picks **fork current session** (carries context) or **fresh** at entry.
- **FR-FILE.0.7** "Send" reads the target file, sends its content to the parent session as the next user message, then disposes the ephemeral session + temp dir.
- **FR-FILE.0.8** Empty target file is allowed; the meta-prompt makes this explicit to the worker.
- **FR-FILE.0.9** File Mode MUST be addressable via URL/query param — any file path works, not only feature-specific paths.
- **FR-FILE.0.10** All File-Mode subclasses MUST share one common base on BOTH frontend and backend; kept open for extension.
- **FR-FILE.0.11** **Session-creation entry.** The new-session modal MUST expose "Start in File Mode" as a first-class option (not only via project-tree click after the fact). Selecting it MUST present a **file picker** (see FR-FILE.0.12) and create the session already in File Mode with the picked file loaded.
- **FR-FILE.0.12** **File picker.** A reusable file-picker component is provided to any File-Mode entry point that needs one. It MUST support: keyboard navigation, **search by substring of path** (incremental), display of relative-to-cwd paths, and gitignore-aware filtering. The picker is the same component for session-creation entry and any future File-Mode entry needing file selection.
- **FR-FILE.0.13** **Overlay fills the main panel.** The File-Mode overlay MUST occupy the full available height/width of the main panel — no empty area below the editor, no fixed bottom hint bar. Action buttons (Discard / Done) live in the top bar; any contextual help belongs to the file viewer or chat side, not a persistent footer.
- **FR-FILE.0.14** **Default view = file, not diff.** On entering File Mode, the file panel MUST default to "file" view (current contents). "Diff" view is opt-in via the view-mode toggle. Rationale: in normal editing flow the file view is the dominant tool; diff is the occasional check.
- **FR-FILE.0.15** **Default split + free resize.** The chat (left) and file (right) panes MUST start at a 50/50 split on entry. The divider is resizable in both directions: the file pane MAY grow wider than chat, AND the chat pane MAY shrink narrower than its normal-mode minimum width. Pure UI prefs only (panel width can live in localStorage per FR-FILE.0.* policy, since it's not backend-persistent state).
- **FR-FILE.0.16** **Markdown view/edit toggle for `.md` files.** When the target file is markdown, the file panel MUST default to rendered-markdown view (formatted, with tags formatting applied). A **double-click** anywhere in the rendered view MUST switch the panel into raw-text edit mode. After the last edit, a **10-second idle debounce** MUST automatically return the panel to rendered view. The user MAY still toggle modes explicitly. This applies in addition to FR-FILE.0.14: "file view" for `.md` files means "rendered-markdown view by default" (not raw text).

#### 4.1.1 Prompt-engineering (delta)
- **FR-FILE.1.1** Target file is a temp `prompt.md`; meta-prompt: refine the file in place.
- **FR-FILE.1.2** Entry: "Engineer my prompt" button (visible on non-empty draft); top-bar badge shows fork-vs-fresh origin.

#### 4.1.2 Generic file-edit (delta)
- **FR-FILE.2.1** Entry: opening any file from the project tree starts a fresh File-Mode session with that file loaded.
- **FR-FILE.2.2** Operates on the actual file on disk; diff baseline = entry-time content.

#### 4.1.3 Project-settings AI-Edit (delta)
- **FR-FILE.3.1** Project settings panel lists all Claude-relevant files (CLAUDE.md, `.claude/settings*.json`, skills, hooks, keybindings, launch config) grouped by category.
- **FR-FILE.3.2** Each file is openable for plain view (read-only) and "AI Edit" (routes into 4.1.2).
- **FR-FILE.3.3** Missing files MUST be shown dimmed with a "Create" action.

#### 4.1.4 Project-harness-file edit (delta)
- **FR-FILE.4.1** Same shape as 4.1.3 scoped to harness files the agent itself uses (skills, hooks, project claude config). Differs only in the file picker.

#### 4.1.5 Plan mode (proposed, delta)
- **FR-FILE.5.1** (proposed) Target is a plan document; meta-prompt: iteratively refine the plan; finalize/dispose per 4.1.0.

---

### 4.2 Orchestration Mode

`OrchestrationStrategy` ABC on FE + BE. Native, Manager, Supervisor are siblings; **Supervisor MUST NOT inherit from Manager**.

#### 4.2.0 Base rules
- **FR-ORCH.0.1** User picks orchestration mode per session: `native` | `manager` | `supervisor`.
- **FR-ORCH.0.2** Switching the sidebar selector only affects what new sessions default to and which panels are visible; it MUST NOT mutate existing sessions.
- **FR-ORCH.0.3** Mode-specific logic lives in subclasses; shared shell code MUST contain no `if mode == ...`.
- **FR-ORCH.0.4** Each strategy provides hooks for: dispatching a user prompt, applying stream events to running content, rewinding the session(s) it owns, producing a per-turn trace.

#### 4.2.1 Native (delta)
- **FR-ORCH.1.1** Exactly one persistent claude/gemini CLI session per BC session; no MCP, no prompt wrapping; resumed by `session_id`.
- **FR-ORCH.1.2** Rewind applies to the single underlying CLI session.

#### 4.2.2 Manager (delta)
- **FR-ORCH.2.1** A persistent manager session delegates to worker BC sessions via the in-process `delegate` SDK MCP tool.
- **FR-ORCH.2.2** `delegate` MUST be registered via `create_sdk_mcp_server` (in-process) — never stdio/HTTP MCP — because `claude -p --print` ignores external MCPs.
- **FR-ORCH.2.3** Manager view MUST surface worker activity live — delegate calls and each worker's full event trace, not just summaries.
- **FR-ORCH.2.4** Rewind applies to the manager session AND every involved worker session.

#### 4.2.3 Supervisor (delta)
- **FR-ORCH.3.1** One user-facing supervisor session paired 1:1 with an internal `supervisor_worker`; user prompts default to the worker.
- **FR-ORCH.3.2** A `Stop` hook on the worker triggers the backend to ask the supervisor for `CONTINUE`/`FIX`/`DONE`, capped at 3 verdicts/turn.
- **FR-ORCH.3.3** Supervisor verdict prompt MUST be adversarial: assume the worker is lazy and declares DONE prematurely.
- **FR-ORCH.3.4** Supervisor's own internal work (verdict request/reply, prompt fed to it) MUST be ingested and rendered under a supervisor tag in the supervisor panel — never as fake "user" prompts.
- **FR-ORCH.3.5** Supervisor MAY auto-hand its review back to the worker as a follow-up prompt.
- **FR-ORCH.3.6** User MAY toggle send target between worker (default) and supervisor; the toggle MUST work even on an empty input.
- **FR-ORCH.3.7** Supervisor-injected prompts MUST be tagged "Supervisor"/"Worker" with distinct icons — never "User".
- **FR-ORCH.3.8** **Panel invariant**: each split panel renders ONLY its corresponding native session's messages — no cross-session mixin. (See INV-13.)
- **FR-ORCH.3.9** Split panel shares ONE vertical time axis: row `y` in the worker pane represents the same moment as row `y` in the supervisor pane; gaps in one pane render as whitespace (the side was idle), never collapsed. (See INV-23.)
- **FR-ORCH.3.10** Supervisor receives a line range in the worker's native jsonl so it can Grep/Read for evidence.
- **FR-ORCH.3.11** Cap-hit / verdict-path crashes MUST emit a `supervisor_event` WS frame — no silent failures.
- **FR-ORCH.3.12** Rewind applies to BOTH sides.

---

### 4.3 Provider

#### 4.3.0 Base rules
- **FR-PROV.0.1** A provider exposes `config_dir`, `build_env`, model-listing, and credential resolution from Keychain.
- **FR-PROV.0.2** Adding/configuring a provider MUST be a wizard (only at add-time); secrets MUST go to macOS Keychain, never JSON config files.
- **FR-PROV.0.3** Model lists MUST be fetched dynamically (`GET {base_url}/v1/models` for api_key) or from static aliases (subscription), plus user-entered custom names.
- **FR-PROV.0.4** Native-session ingestion MUST resolve the projects root through the active provider's `config_dir`, never via a fixed `~/.claude`.
- **FR-PROV.0.5** Model selection MUST round-trip to the backend immediately as part of the persistent session record.
- **FR-PROV.0.6** The model is set at session start and applies for the whole lifecycle.

#### 4.3.1 ClaudeProvider (delta)
- **FR-PROV.1.1** Two credential variants: subscription (OAuth), api_key (with custom `base_url`, e.g. z.ai).
- **FR-PROV.1.2** When `CLAUDE_CONFIG_DIR` is set (e.g. `~/.claude-zai`), `config_dir` returns that path; all native paths follow.
- **FR-PROV.1.3** Default model: `claude-opus-4-8[1m]`.

#### 4.3.2 GeminiProvider (delta)
- **FR-PROV.2.1** Subscription only; spawns `gemini` via the gemini runner.

#### 4.3.3 Generic Agent (future, delta)
- **FR-PROV.3.1** (evolution) Reserved for a generic non-Claude/Gemini agent; drives the `claude_*` → `agent_*` naming sweep on shared bases.

---

### 4.4 Session

#### 4.4.0 Base rules
- **FR-SESS.0.1** Sessions persist per-root tree as one JSON file; embedded `forks` array — never separate top-level files.
- **FR-SESS.0.2** Sessions MUST be restorable across backend restarts; in-flight runs MUST be recovered from `events.jsonl` + `complete.json` without losing events.
- **FR-SESS.0.3** `cwd` is immutable post-creation.
- **FR-SESS.0.4** Session names editable; AI-generated titles MUST update via WS without manual refresh.
- **FR-SESS.0.5** Archive goes through the backend, not a frontend-only filter.
- **FR-SESS.0.6** Stop/dismiss/delete MUST kill all associated subprocesses (runner, tailers, side coordinators) — no zombies.
- **FR-SESS.0.7** Stopped runs MUST NOT be marked failed on transient WS disconnects. The detached runner outlives disconnects.
- **FR-SESS.0.8** "+ New" MUST use the selected project's cwd, not free-floating cwd state.

#### 4.4.1 Regular user session (delta) — visible in sidebar; URL-addressable; receives user prompts directly.
#### 4.4.2 Supervisor-worker (delta) — paired 1:1 with a supervisor session; each side persists its own history on its own native session (FR-ORCH.3.8).
#### 4.4.3 Engineering-worker (delta) — ephemeral; created on entering 4.1.1; disposed on Send; hidden from sidebar.
#### 4.4.4 File-edit-worker (delta) — ephemeral; created on entering 4.1.2 – 4.1.4; disposed on Send; hidden from sidebar.
#### 4.4.5 Plan-worker (proposed, delta) — same shape as 4.4.3/4.4.4 but for plan documents.

---

### 4.5 Worker (per-cwd registry)

#### 4.5.0 Base rules
- **FR-WORK.0.1** Workers are registered per-cwd at `<ba_home>/workers/<encoded-cwd>.json`. Schema v2; no migrations; old shapes raise.
- **FR-WORK.0.2** Each (caller, worker) pair maintains its own private claude-session fork accumulating context across delegations.
- **FR-WORK.0.3** Workers list / approvals / per-cwd state MUST be shared between UI and CLI.
- **FR-WORK.0.4** Worker creation may include an optional **init prompt** that primes the worker on its scope before any user task. (proposed)
- **FR-WORK.0.5** Nested delegations MUST auto-deny fresh-worker creation; only top-level may request a fresh worker. Nested resumes are allowed.
- **FR-WORK.0.6** Worker spawns MUST minimize initial token payload.

#### 4.5.1 User-marked existing session (delta) — user MAY mark any existing BC session as a worker for its cwd; no approval needed.

#### 4.5.2 Freshly created worker (delta)
- **FR-WORK.2.1** Fresh creation MUST require user approval via an inline approval card.
- **FR-WORK.2.2** Agent picks proposed description + orchestration mode; user MAY edit description and override mode at approval time.

---

### 4.6 Tailer

#### 4.6.0 Base rules
- **FR-TAIL.0.1** A tailer reads provider-native JSONL line-by-line and emits normalized events to the ingestion path.
- **FR-TAIL.0.2** Tailers MUST be cleanly stoppable on session deletion; no leaked watches or CPU loops.
- **FR-TAIL.0.3** Sub-agent (`agent-*.jsonl`) files MUST be discovered and tailed with the correct `parent_tool_use_id` so events nest under their parent tool call.

#### 4.6.1 ClaudeTailer (delta) — tails `<config_dir>/projects/<encoded-cwd>/<sid>.jsonl` + sibling `subagents/`; resolves `<config_dir>` from active provider, not a hardcoded `~/.claude`.
#### 4.6.2 GeminiTailer (delta) — tails Gemini-native JSONL with Gemini-specific event shapes.

---

### 4.7 Ingestion path (singleton)

- **FR-ING.0.1** Live and replay/restore share the same per-event ingest function; replay differs only by an explicit `is_replay` flag that gates side-effects (no WS rebroadcast, no double-write).
- **FR-ING.0.2** Events MUST be deduplicated by UUID across all sources (orchestrator path, file tailer, run-recovery replay).
- **FR-ING.0.3** Each event in `events.jsonl` MUST carry an `msg_id` so per-message backfill is a deterministic lookup, not heuristic turn-grouping.
- **FR-ING.0.4** Cross-stream interleaving uses k-way merge by timestamp (NOT flat timestamp sort), preserving per-stream order — subagent events can't land above their parent tool call. (See INV-22, ADR-9.)
- **FR-ING.0.5** **Same projection** feeds REST `GET /api/sessions/{id}` and WS `messages_replay`/`messages_delta`; live and refresh views MUST render identically.
- **FR-ING.0.6** Errored agent messages MUST be retained and displayed, not silently dropped.

---

### 4.8 Event / Message

#### 4.8.0 Base rules
- **FR-EVT.0.1** Every event carries `uuid`, `parent_uuid`, `msg_id`, `timestamp`, `entity_id`, `kind`.
- **FR-EVT.0.2** Timestamps render small-font aligned to the title.
- **FR-EVT.0.3** Consecutive same-`entity_id` events MUST group visually with collapse/expand headers.
- **FR-EVT.0.4** Collapse/expand animates; every event item has jump-to-parent navigation (chevron).
- **FR-EVT.0.5** "Fast expand all" exists; expand-all within one group expands only that subtree.
- **FR-EVT.0.6** Collapsed groups (prompt groups AND sub-task groups, see FR-EVT.0.7) MUST show the LAST event in the group inline-expanded (not `...`), equivalently in live and persisted views.
- **FR-EVT.0.7** **Sub-task group rendering.** A "sub-task" is any nested unit of work spawned by the main agent: either (a) a native subagent (Task/Agent tool, `subagents/agent-*.jsonl`) or (b) a BC/BA worker delegation (`delegate` MCP call in manager mode invoking a child BC session). Both MUST render as a SINGLE collapsible block nested under their parent `tool_call`/`delegate` event, regardless of ingestion source. The block carries: who's running it (entity id + name), running/done status, elapsed time, and child events ordered per INV-22.
- **FR-EVT.0.8** **Auto-collapse on terminal event.** When a sub-task group emits its terminal event (success, error, cancellation, supervisor verdict-done — whichever ends that unit of work), the group MUST auto-collapse, leaving the LAST child event inline-expanded per FR-EVT.0.6. Auto-collapse fires once on transition to terminal state; the user MAY re-expand manually and the group MUST then stay expanded (no re-collapse) for the rest of the session. Live and replay/refresh views MUST converge on the same collapsed-state-after-terminal idiom (consistent with INV-15).

#### Subclasses
- **4.8.1 user_prompt** — owns the user-message lifecycle; orch-injected prompts MUST NOT be persisted as user_prompt.
- **4.8.2 assistant_text** — streaming content updates mid-turn in ALL modes (manager included; see OQ-3).
- **4.8.3 tool_call** — headers MUST be expandable when truncated; full command on demand.
- **4.8.4 tool_result** — Agent/Task tool calls MUST carry `parent_tool_use_id`.
- **4.8.5 subagent_event** — native Task/Agent subagent events from `subagents/agent-*.jsonl`; ingested through the same path as main-agent events; renders as a sub-task group (FR-EVT.0.7) under its parent `tool_call`; ordered via k-way merge with causal parent-before-child rule (INV-22); auto-collapses on terminal event per FR-EVT.0.8.
- **4.8.6 supervisor_event** — rendered under supervisor tag (FR-ORCH.3.4); verdict-cap/crash MUST emit one (FR-ORCH.3.11).
- **4.8.7 worker_event** — BC/BA worker delegation events streamed from a child BC session into the parent's manager view; renders as a sub-task group (FR-EVT.0.7) under the parent's `delegate` call — identical UX to 4.8.5; never as plain "user" in supervisor mode (FR-ORCH.3.7); auto-collapses on terminal event per FR-EVT.0.8.
- **4.8.8 rearranger_update** — own per-session WS callback registry; does not block the main turn.
- **4.8.9 queue_op** — tracks user-message queue transitions.

---

### 4.9 Approval

#### 4.9.0 Base rules
- **FR-APPR.0.1** Pending approvals MUST be disk-backed under `ba_home()` and survive backend restart.
- **FR-APPR.0.2** State transitions MUST be fcntl-locked so multi-tab clicks are idempotent.
- **FR-APPR.0.3** Resolving in one tab MUST clear the card in every other tab via WS — never via stale local state.
- **FR-APPR.0.4** No auto-deny; approval waits indefinitely (24h runner block tolerated).

#### 4.9.1 Fresh-worker approval (delta)
- **FR-APPR.1.1** Card MUST appear inline in chat where the delegate call originated — never as a modal sidebar.
- **FR-APPR.1.2** Payload: agent's justification, proposed description, proposed orchestration mode.
- **FR-APPR.1.3** User MAY edit description and override mode before approving.

---

## 5. User-facing flows (job stories + Gherkin acceptance criteria)

### Forking

#### JS-FORK.1 Fork at a conversation point
**When** I'm reviewing past output and want to try a different branch from message N, **I want to** fork the session at that exact point, **so I can** explore an alternative without losing the original.
**Acceptance:**
- **Given** a session with messages, **when** I trigger fork at message N, **then** a sibling pane opens sharing history above the fork point.
- **Given** the fork just opened, **when** I look at the pane stack, **then** the new fork has focus and the next prompt I send goes only there.
(covers FR-FORK.1, FR-FORK.5)

#### JS-FORK.2 Side-by-side pane comparison
**When** I have forks open, **I want to** see all panes side-by-side below the fork point, **so I can** compare branches without switching tabs.
**Acceptance:**
- **Given** N forks exist below `fork_point_seq`, **when** I open the session, **then** N+1 panes render side-by-side.
- **Given** a non-focused pane has activity from a fresh run, **when** an event arrives, **then** that pane updates live (not only on refresh).
(covers FR-FORK.2, FR-FORK.4)

#### JS-FORK.3 Focus selector and close
**When** I want a different pane to receive my next prompt, **I want to** click a focus selector on it, **so I can** drive whichever branch I'm currently working on.
**Acceptance:**
- **Given** a multi-pane view, **when** I click the focus selector on pane B, **then** pane B becomes focused and future prompts route to it.
- **Given** a pane I'm done with, **when** I click its close button, **then** it stays visible and persisted but no longer accepts new prompts.
(covers FR-FORK.3)

#### JS-FORK.4 Nested forking
**When** I've already forked once, **I want to** fork again inside one of the panes, **so I can** drill deeper without restructuring.
**Acceptance:**
- **Given** an existing fork pane, **when** I fork inside it at message M, **then** a nested fork appears and auto-acquires focus.
(covers FR-FORK.5)

### Message lifecycle

#### JS-MSG.1 Queue while a turn is running
**When** the agent is still working on the previous turn, **I want to** type and send the next prompt anyway, **so I can** keep my train of thought.
**Acceptance:**
- **Given** a running turn, **when** I press Send, **then** the message enters `user_message_queued` and runs after the current turn finishes.
- **Given** a queued message exists, **when** I view the chat, **then** I can see it tagged as queued (vs interrupt).
(covers FR-MSG.1, FR-MSG.2, FR-MSG.3)

#### JS-MSG.2 Optimistic send with ack replacement
**When** I press Send, **I want to** see my message immediately, **so I can** keep working without waiting for a roundtrip.
**Acceptance:**
- **Given** I press Send, **when** the click registers, **then** the message appears optimistically in the transcript.
- **Given** the backend emits `user_message_persisted`, **when** the WS event arrives, **then** the optimistic entry is replaced (not duplicated) by the persisted one.
(covers FR-MSG.4)

#### JS-MSG.3 Image attachments survive forwarding
**When** I attach an image to a prompt that gets forwarded through engineering / orch injection, **I want to** have the image reach the provider, **so I can** rely on attachments not silently disappearing.
**Acceptance:**
- **Given** a prompt with an image attachment, **when** the prompt is forwarded by File-Mode or an orch strategy, **then** the image is preserved in the provider input.
(covers FR-MSG.5)

#### JS-MSG.4 No auto-scroll when reading back
**When** I scroll up to read earlier output during a streaming turn, **I want to** stay where I am, **so I can** read without being yanked back to bottom.
**Acceptance:**
- **Given** I scroll up while a turn streams, **when** new events arrive, **then** the view stays at my scroll position and the anchor toggle unchecks.
- **Given** I'm at the bottom and the anchor is checked, **when** new events arrive, **then** the view follows.
(covers FR-MSG.6)

#### JS-MSG.5 Draft persistence across reloads
**When** I'm halfway through composing a message and reload, **I want to** have my draft survive, **so I can** pick up exactly where I left off.
**Acceptance:**
- **Given** I'm typing in the composer, **when** keystrokes settle, **then** the draft is debounced-synced to the backend.
- **Given** I reload the page, **when** the session reopens, **then** the draft is exactly as I left it.
(covers FR-MSG.7)

#### JS-MSG.6 Running-entity status bar
**When** multiple entities are active (manager, workers, supervisor), **I want to** see who's running and how long since their last event, **so I can** tell if something is stuck.
**Acceptance:**
- **Given** ≥1 entity is running, **when** I look at the bottom status bar, **then** I see each running entity with time-since-last-event.
(covers FR-MSG.8)

### Comments on prompts and file selections

#### JS-CMT.1 Selection popover and side panel
**When** I select text in an assistant message or a File-Mode viewer, **I want to** trigger the same "Copy / Comment" popover, **so I can** attach a note to that span anywhere.
**Acceptance:**
- **Given** I select text in chat or the file viewer, **when** the selection settles, **then** the Copy/Comment popover appears.
- **Given** I add a comment, **when** the side panel opens, **then** the comment is anchored to the source with a connector line and a noticeable highlight.
(covers FR-CMT.1, FR-CMT.2, FR-CMT.3, FR-CMT.4, FR-CMT.5)

#### JS-CMT.2 Persisted comments auto-attach
**When** I add comments and send my next prompt, **I want to** have them included automatically, **so I can** treat them as inline review notes without a separate "send".
**Acceptance:**
- **Given** comments exist on this session, **when** I send a prompt, **then** they ride along with the prompt and are then deleted.
- **Given** I reload mid-comment, **when** the session reopens, **then** my comments are still there.
(covers FR-CMT.6)

#### JS-CMT.3 File:line anchors over full embedding
**When** I comment on a large file selection, **I want to** see the model receive a `file:line(:col)` pointer instead of a re-embedded blob, **so I can** keep prompts compact.
**Acceptance:**
- **Given** I comment on lines L1..L2 of a file in File Mode, **when** the prompt is built, **then** the comment carries a `file:line(:col)` range pointer.
(covers FR-CMT.7)

### Retry / Rewind

#### JS-RW.1 Right-click rewind toolbox
**When** I want to undo a turn or retry from a point, **I want to** right-click a message and pick from a popover, **so I can** rewind without leaving the chat.
**Acceptance:**
- **Given** I right-click any message, **when** the toolbox opens, **then** I see "Rewind" and "Rewind with files" anchored at the cursor.
- **Given** the popover is open, **when** I click outside or press Esc, **then** it closes; clicking an option asks for confirmation (irreversible).
(covers FR-RW.1)

#### JS-RW.2 Rewind with files via native CLI
**When** I rewind to a snapshot that also rolled back files, **I want to** have the file state restored too, **so I can** truly redo from that moment.
**Acceptance:**
- **Given** a message has a snapshot UUID, **when** I pick "Rewind with files", **then** the CLI native `--rewind-files <uuid>` is used (not jsonl mutation).
- **Given** no snapshot UUID, **when** I open the toolbox, **then** "Rewind with files" is disabled with a tooltip.
(covers FR-RW.2, FR-RW.3)

#### JS-RW.3 Retry actually triggers
**When** I click "Retry" on a finished assistant message, **I want to** have it rewind-and-retry, **so I can** rely on the button doing what its label says.
**Acceptance:**
- **Given** a finished assistant message, **when** I click Retry, **then** the session rewinds to before that message and runs again.
(covers FR-RW.5)

### Fresh-worker approval

#### JS-APPR.1 Inline approval card
**When** the agent asks to spawn a fresh worker, **I want to** approve or override inline where the delegate call happened, **so I can** decide in context.
**Acceptance:**
- **Given** a delegate call requests a fresh worker, **when** the approval is raised, **then** an inline card appears in chat (not a modal sidebar) showing justification, proposed description, and proposed mode.
- **Given** the approval is pending, **when** I edit the description and override the orchestration mode, **then** my edits stick on approval.
(covers FR-APPR.1.1, FR-APPR.1.2, FR-APPR.1.3, FR-WORK.2.2)

#### JS-APPR.2 Multi-tab convergence
**When** I have the same session open in multiple tabs and approve in one, **I want to** see the card disappear in all of them, **so I can** trust that the decision is global.
**Acceptance:**
- **Given** an approval card is showing in tabs A and B, **when** I approve in tab A, **then** the card disappears in tab B via WS (not on next refresh).
(covers FR-APPR.0.3)

#### JS-APPR.3 Indefinite wait
**When** I leave an approval pending, **I want to** have it wait until I deal with it, **so I can** step away without auto-deny biting me.
**Acceptance:**
- **Given** an unanswered approval, **when** time passes, **then** no auto-deny fires and the runner keeps blocking up to 24h.
(covers FR-APPR.0.4)

### Entering File Mode

#### JS-FILE.1 Open any file from the project tree
**When** I click a file in the project tree, **I want to** enter generic file-edit mode with that file loaded, **so I can** AI-edit it without manual setup.
**Acceptance:**
- **Given** I click a project file, **when** File Mode opens, **then** the main panel shows chat (left) + file viewer with diff baseline (right).
(covers FR-FILE.0.1, FR-FILE.2.1)

#### JS-FILE.2 "Engineer my prompt"
**When** I'm drafting a non-empty prompt, **I want to** click "Engineer my prompt", **so I can** iterate on the wording with an ephemeral worker before sending.
**Acceptance:**
- **Given** my draft is non-empty, **when** I click "Engineer my prompt", **then** an ephemeral prompt-engineering session opens on `prompt.md` with a fork-vs-fresh badge.
(covers FR-FILE.1.1, FR-FILE.1.2)

#### JS-FILE.3 Project-settings AI-Edit
**When** I want to edit a Claude config file (CLAUDE.md, settings.json, a skill, a hook), **I want to** open project settings and pick "AI Edit", **so I can** route into the same File Mode flow.
**Acceptance:**
- **Given** I open project settings, **when** the panel lists configs by category, **then** missing files show dimmed with a Create action.
- **Given** I click "AI Edit" on a listed file, **when** File Mode opens, **then** it behaves identically to generic file-edit.
(covers FR-FILE.3.1, FR-FILE.3.2, FR-FILE.3.3)

#### JS-FILE.4 Leave and return without losing work
**When** I navigate away from an active File-Mode session, **I want to** come back and find it intact, **so I can** treat it as a real workspace.
**Acceptance:**
- **Given** an active File-Mode session, **when** I leave and return via URL or sidebar, **then** chat, file content, and diff are restored.
(covers FR-FILE.0.4, FR-FILE.0.9)

#### JS-FILE.5 Send finalizes and disposes
**When** I'm done iterating in File Mode and hit Send, **I want to** have the file content delivered to the parent session and the ephemeral worker disposed, **so I can** return cleanly.
**Acceptance:**
- **Given** I press Send in File Mode, **when** the action completes, **then** the parent session receives the file content as the next user message and the ephemeral session + temp dir are gone.
- **Given** the target file is empty, **when** I press Send, **then** the action still proceeds (the worker's meta-prompt covers this case).
(covers FR-FILE.0.7, FR-FILE.0.8)

#### JS-FILE.6 Start a session directly in File Mode
**When** I'm creating a new session and I already know I want to work on a specific file, **I want to** pick "Start in File Mode" in the new-session modal and choose the file there, **so I can** skip the "create normal session → then enter File Mode" two-step.
**Acceptance:**
- **Given** I open the new-session modal, **when** I look at the options, **then** "Start in File Mode" is a first-class toggle next to orchestration mode / model.
- **Given** I enable "Start in File Mode", **when** the modal expands, **then** a file picker (FR-FILE.0.12) appears with incremental substring search and gitignore-aware filtering, listing paths relative to cwd.
- **Given** I pick a file and confirm, **when** the session opens, **then** it's already in File Mode with the file viewer auto-opened and the file loaded (FR-FILE.0.1, FR-FILE.2.1).
(covers FR-FILE.0.11, FR-FILE.0.12, FR-FILE.0.1)

### Trace inspection

#### JS-TR.1 Every turn produces a trace
**When** any turn completes (or crashes), **I want to** have a trace available, **so I can** inspect what happened regardless of mode.
**Acceptance:**
- **Given** a finished turn in any orchestration mode, **when** I open the trace, **then** events, tool calls, and token usage are present.
- **Given** a crash-recovered turn, **when** the trace is built, **then** it's produced via a fresh `TraceCollector` replay (not skipped).
(covers FR-TR.1, FR-TR.3)

#### JS-TR.2 Sub-task groups (native subagent or BC worker) auto-collapse with last event inline
**When** a turn spawns a sub-task — either a native Task/Agent subagent or a delegated BC worker — **I want to** see it as one collapsible group nested under the parent, auto-folding when it's done with the last event left visible, **so I can** scan the transcript without manually collapsing 30 child events while still seeing "what did it end on".

**Acceptance:**
- **Given** a parent agent calls a Task tool that spawns a native subagent, **when** the subagent emits events, **then** they appear as a single collapsible group under the parent `tool_call` row.
- **Given** a manager agent calls `delegate` on a BC worker, **when** the worker emits events, **then** they appear as a single collapsible group under the `delegate` row — visually identical to the native-subagent case (same component, only the source-tag icon differs).
- **Given** the sub-task is still running, **when** I view the parent transcript, **then** the group is expanded and shows live child events with a running indicator.
- **Given** the sub-task emits its terminal event, **when** that event lands, **then** the group auto-collapses ONCE, leaving the LAST child event inline-expanded (same idiom as collapsed prompt groups).
- **Given** I manually expand an auto-collapsed group, **when** further events arrive or I scroll, **then** it stays expanded for the rest of the session (no re-collapse).
- **Given** I reload the page, **when** the session re-renders, **then** completed sub-task groups come back in the collapsed-with-last-event-inline state (live and refresh views converge).
**Trace-tree acceptance (carry-over from earlier draft):**
- **Given** a turn with subagent activity, **when** I view the trace, **then** subagent events appear inside the parent tool call (not flat).
(covers FR-TR.2)

#### JS-TR.3 trace_cli inspects captured traces
**When** I'm debugging from a terminal, **I want to** run `trace_cli.py` against a captured trace, **so I can** inspect without the UI.
**Acceptance:**
- **Given** a captured trace, **when** I run `trace_cli.py`, **then** I can list and inspect entries.
(covers FR-TR.4)

### CLI driver

#### JS-CLI.1 REPL or one-shot, with auto-detect
**When** I run `python cli.py`, **I want to** use it as REPL or one-shot, **so I can** drive sessions from the terminal.
**Acceptance:**
- **Given** I run `cli.py`, **when** no args, **then** I get a REPL; **when** I pass `-p`, **then** I get a one-shot reply.
- **Given** a backend is running on :8000, **when** cli.py starts, **then** it connects as a WS client; otherwise it starts uvicorn in-process and drives `Coordinator` directly.
(covers FR-CLI.1, FR-CLI.2, FR-CLI.5)

#### JS-CLI.2 CLI sessions appear in UI
**When** I create a session from the CLI, **I want to** see it in the BC UI sidebar, **so I can** switch between drivers without losing visibility.
**Acceptance:**
- **Given** I create a session via CLI, **when** I open the UI, **then** the session shows up identically to a UI-created one.
(covers FR-CLI.3)

#### JS-CLI.3 --json output for scripting
**When** I want to script tests against CLI output, **I want to** request JSON, **so I can** assert on structured fields.
**Acceptance:**
- **Given** I pass `--json`, **when** the CLI renders, **then** it emits a JSON representation of what it would render.
(covers FR-CLI.4)

### Supervisor mode

#### JS-SUP.1 Toggle send target between worker and supervisor
**When** I'm in supervisor mode and want to talk to the supervisor directly, **I want to** toggle the send target, **so I can** address either side without ambiguity.
**Acceptance:**
- **Given** supervisor mode, **when** I toggle the send target, **then** the next prompt routes to the picked entity.
- **Given** the input is empty, **when** I toggle, **then** the toggle still works.
(covers FR-ORCH.3.6)

#### JS-SUP.2 Supervisor verdict visible under supervisor tag
**When** the supervisor issues a CONTINUE/FIX/DONE verdict, **I want to** see it tagged as supervisor work, **so I can** distinguish it from my own user prompts.
**Acceptance:**
- **Given** a Stop hook triggers a verdict request, **when** the supervisor responds, **then** the verdict and the prompt fed to it are rendered under a Supervisor tag in the supervisor panel.
- **Given** the verdict cap is hit or the path crashes, **when** the event lands, **then** a `supervisor_event` WS frame is emitted (not silent failure).
(covers FR-ORCH.3.3, FR-ORCH.3.4, FR-ORCH.3.7, FR-ORCH.3.11)

#### JS-SUP.3 Split panel chronology
**When** I'm reading the split panel, **I want to** have one shared vertical time axis across both panes, **so I can** see worker and supervisor events in true chronological order without losing causal alignment.
**Acceptance:**
- **Given** the split panel is open, **when** I scroll, **then** both panes scroll together against one shared time axis, each pane rendering only its own native session's messages.
- **Given** the worker emits N events during a stretch where the supervisor is idle, **when** I look at the supervisor pane over that range, **then** I see preserved whitespace (not collapsed) so row-height = real-time alignment holds.
- **Given** a supervisor verdict lands at time T after a worker tool call at time T-Δ, **when** I look at the panel, **then** the verdict row sits below the worker tool-call row in the shared axis.
(covers FR-ORCH.3.8, FR-ORCH.3.9, INV-13, INV-23)

### Multi-tab convergence

#### JS-MULTI.1 Approvals converge across tabs
**When** I resolve any approval in one tab, **I want to** see it disappear in all other tabs, **so I can** trust the state is global.
**Acceptance:**
- **Given** an approval card in tabs A and B, **when** I resolve in A, **then** B's card clears via WS (not stale local state).
(covers FR-APPR.0.3)

#### JS-MULTI.2 Archive, title, worker creation converge
**When** I archive a session, rename its title, or a worker gets created, **I want to** see all open tabs reflect the change, **so I can** never rely on a stale local view.
**Acceptance:**
- **Given** the same session open in tabs A and B, **when** archive/title/worker change in A, **then** the change is mirrored in B live.
(covers FR-SESS.0.4, FR-SESS.0.5)

---

## 6. Invariants

- **INV-1 Single ingestion code path.** Live and replay/restore share the same per-event ingest function, gated by `is_replay`. **Why:** divergent paths silently drift derived state, WS broadcasts, and persistence — codified in CLAUDE.md. **How to verify:** grep shared ingest module for an `is_replay` parameter; no parallel "restore-only" ingest functions.
- **INV-2 Backend is single source of truth.** Persistent state lives on disk via `*_store`; frontend holds only the optimistic ack-bridge. **Why:** user repeatedly enforced — drift is wrong data. **How to verify:** scan frontend for `useState` of persistent fields (workers, sessions, projects, model, mode).
- **INV-3 Every backend mutation emits a WS event.** Any persisted change the frontend cares about pushes a WS frame. **Why:** avoids manual-refresh polling and multi-tab stale state. **How to verify:** each `*_store.save` / mutation path has a paired `broadcast` call.
- **INV-4 Persistence goes through `paths.ba_home()`.** Never hardcode `~/.better-claude/` or `~/.better-agent/` in code or scripts. **Why:** tests and dev scripts have clobbered real state. **How to verify:** runtime path construction imports `paths.ba_home()` instead of joining either default directory.
- **INV-5 Native-session ingestion resolves projects root via active provider's `config_dir`.** Tailers/backfill use `provider.config_dir`, not a fixed `~/.claude`. **Why:** z.ai uses `~/.claude-zai`; mismatched roots break ingestion. **How to verify:** search tailers for hardcoded `.claude` literals.
- **INV-6 Schema bumps raise on old shape.** No silent migrations. **Why:** user pinky-swore against migrations. **How to verify:** each store's load path has an explicit version check + raise.
- **INV-7 Orchestration mode differences live in `OrchestrationStrategy` subclasses.** No `if mode == ...` chains in shared shell code. **Why:** per-mode branches breed inconsistent behavior. **How to verify:** `grep -nE 'if .*mode ==' backend/` and FE equivalent return only strategy-dispatch sites.
- **INV-8 Supervisor is a SIBLING of Manager.** `SupervisorStrategy` MUST NOT inherit from `ManagerStrategy`. **Why:** user mandate; conceptually distinct. **How to verify:** read class hierarchy of both strategies.
- **INV-9 All File-Mode subclasses share one base on FE+BE.** One base drives prompt-engineering, generic file-edit, settings AI-Edit, harness-edit, plan mode. **Why:** prevents per-feature divergence. **How to verify:** find the shared base in both `backend/orchs/` and `frontend/src/`.
- **INV-10 Workers are per-cwd, not per-session record.** Worker registry keyed by encoded cwd. **Why:** same project across sessions shares workers. **How to verify:** `worker_store.py` paths use `<encoded-cwd>.json`.
- **INV-11 Manager-mode delegate tools use in-process SDK MCP only.** `create_sdk_mcp_server`, never stdio/HTTP MCP. **Why:** `claude -p --print` ignores external MCPs. **How to verify:** no stdio MCP registration for `delegate` in manager paths.
- **INV-12 Nested delegations may only resume — never fresh worker.** Depth>0 auto-denies fresh-worker creation. **Why:** prevents recursive spawning chaos. **How to verify:** nested delegate path checks depth before approval flow.
- **INV-13 Supervisor split panel renders only its native session.** Each panel = exactly one native session's messages. **Why:** cross-mixin breaks identity and chronology. **How to verify:** panel render takes a single session id; no cross-session merge.
- **INV-14 Orch-injected prompts MUST NOT be persisted/rendered as `user_prompt`.** Supervisor- or manager-injected prompts get supervisor/worker tags, not user. **Why:** misleads the user about authorship. **How to verify:** ingestion path branches by source-tag; `user_prompt` reserved for human input.
- **INV-15 Live and refresh views render identically.** REST `GET /api/sessions/{id}` and WS replay/delta use the same projection. **Why:** divergence == bugs that only repro one way. **How to verify:** REST handler and WS replay both call the same backfill function.
- **INV-16 Stop/dismiss/delete kills all associated subprocesses.** No zombies — runner, tailers, side coordinators all torn down. **Why:** leaked processes burn CPU and confuse state. **How to verify:** session-delete path enumerates and kills children.
- **INV-17 Detached runner outlives WS disconnects.** Orchestrator MUST NOT cancel turns on transient drops. **Why:** user repeatedly hit "transient disconnect kills turn" pain. **How to verify:** runner lifecycle has no `ws_disconnect → cancel` hook.
- **INV-18 No silent failures across modes.** Cap-hit / crash paths emit a renderable event. **Why:** user must see why a turn stopped. **How to verify:** every verdict / cap / crash path has an explicit event emit.
- **INV-19 Secrets in macOS Keychain only.** Never write secrets to JSON config files. **Why:** config files end up in screenshots and git. **How to verify:** provider credential write paths target Keychain APIs.
- **INV-20 Tests set `BETTER_AGENT_HOME` to a tempdir before importing backend.** Never let tests touch the developer's real home. Legacy `BETTER_CLAUDE_HOME` remains accepted for older tests. **Why:** real session state has been clobbered. **How to verify:** `conftest.py` / test setup sets env var pre-import.
- **INV-21 Ephemeral sessions MUST NOT appear in the sidebar.** File-Mode subclasses (engineering/file-edit/plan workers) are hidden. **Why:** clutters the user's session list. **How to verify:** sidebar query filters out ephemeral session types.
- **INV-22 Event ordering: per-stream order preserved; cross-stream merged via k-way timestamp merge; causal parent-before-child.** Within a single emitter's stream, original arrival order is law — never re-sorted by timestamp. Across streams (main agent, subagents, supervisor, rearranger, queue ops), interleaving is a k-way merge by timestamp. A subagent event MUST NEVER appear above the parent `tool_call` that spawned it, regardless of timestamp skew. Applies identically to live ingestion, REST backfill, and WS replay. **Why:** subagent JSONLs carry timestamps from when the worker ran — often earlier than the parent tool call — so flat timestamp sort breaks causality and confuses the reader. **How to verify:** ingestion projection uses a per-stream cursor + heap merge (not `sorted(events, key=timestamp)`); add a regression test where a subagent emits with timestamp < parent tool_call timestamp and assert the rendered order is parent → child.
- **INV-24 Sub-task groups render identically regardless of source.** A native Task/Agent subagent (from `subagents/agent-*.jsonl`) and a BC worker delegation (from a `delegate` MCP call) MUST be rendered by the SAME collapsible-group component — same header, same child layout, same auto-collapse-on-terminal behavior (FR-EVT.0.8), same last-child-inline collapsed view (FR-EVT.0.6). The only allowed difference is the source-tag icon/label in the header. **Why:** for the user, both are "a nested unit of work the agent kicked off"; divergent rendering breaks the mental model and makes it harder to scan a transcript. Mirrors the broader base/extension pattern (INV-9). **How to verify:** one shared sub-task-group component in the frontend renders both; there is no separate "SubagentGroup" vs "WorkerGroup" component tree.
- **INV-23 Supervisor split panel uses a single shared vertical time axis.** Both panes render against one shared y-axis so a row at height `y` in the worker pane and a row at height `y` in the supervisor pane represent the same moment in real time. Each pane still renders ONLY its own native session (INV-13) — the shared axis means timeline alignment, not message mixing. Empty stretches in one pane are real (that side was idle) and MUST be preserved as whitespace, not collapsed. **Why:** the whole point of supervisor mode is seeing causality between worker action and supervisor verdict; per-pane independent scrolling destroys that. **How to verify:** split panel renders both columns inside one scroll container with a shared coordinate space; gaps in one column show as blank, not stripped.

---

## 7. Non-Functional Requirements

- **NFR-1 Performance budgets.** Session switch / create SHOULD be fast; progressive paging proposed for large sessions.
- **NFR-2 Token efficiency.** Worker / subagent spawns MUST minimize initial token payload.
- **NFR-3 Reactive backbone direction.** Plan to adopt a reactive library (RxPython considered) for the event-bus / lifecycle pipeline.
- **NFR-4 Decoupled event-bus communication.** Internal modules SHOULD communicate via an event bus to avoid bidirectional dependencies.
- **NFR-5 SOLID + DRY.** Prefer base classes with concrete subclasses (orchestration, tailers, providers, file mode, sessions).
- **NFR-6 Generic terminology / naming sweep.** Shared bases use `agent_*`; concrete Claude things stay `claude_*`.
- **NFR-7 Concise responses everywhere**, including agent subreport templates and supervisor verdict prompts.
- **NFR-8 Headed-toggle for embedded browsers.** Headed must not flicker / jump.
- **NFR-9 No reliance on git reflog** for recovery.
- **NFR-10 No fallback / no silent error swallowing** unless explicitly asked.

---

## 8. Architecture Decision Records (ADRs)

### ADR-1: Single ingestion code path with `is_replay` flag
**Status:** Accepted (2026-05-13)
**Context:** Live event ingestion and on-restart replay diverged early, producing drifted in-memory state and broken WS broadcasts on restored sessions.
**Decision:** Both paths funnel through one per-event ingest function. Replay differs only via an explicit `is_replay` flag gating side-effects (WS rebroadcast, store double-write).
**Consequences:** (+) restored state matches live by construction; (+) new side-effects added once apply to both paths; (−) authors must consciously gate replay-skips inside the shared path instead of forking.
**Related invariants:** INV-1, INV-15.

### ADR-2: Backend as single source of truth; WS-driven multi-tab convergence
**Status:** Accepted (2026-05-13)
**Context:** Frontend `useState` shadow-copies of backend state drifted across tabs and reloads, producing stale-data bugs.
**Decision:** All persistent state lives backend-side in `*_store`; frontend pulls REST snapshots + subscribes to WS deltas. Frontend state is restricted to the optimistic ack-bridge.
**Consequences:** (+) multi-tab convergence is automatic; (+) single audit point for persistence; (−) every backend mutation must remember to emit a WS event.
**Related invariants:** INV-2, INV-3, INV-15.

### ADR-3: `OrchestrationStrategy` ABC with sibling Native / Manager / Supervisor subclasses
**Status:** Accepted (2026-05-13)
**Context:** Per-mode `if` chains in shared shell code produced inconsistent behavior; an earlier draft of Supervisor inheriting from Manager leaked manager semantics into supervisor flows.
**Decision:** Mode-specific logic lives in subclasses of `OrchestrationStrategy` on both FE and BE. Native, Manager, and Supervisor are siblings.
**Consequences:** (+) new modes plug in without touching shared code; (+) Supervisor stays conceptually independent of Manager; (−) strategy interface needs upfront thought for new hooks.
**Related invariants:** INV-7, INV-8.

### ADR-4: File Mode as a shared base across prompt-engineering / file-edit / settings-edit / plan
**Status:** Accepted (2026-05-13)
**Context:** Separate feature-specific bases for prompt-engineering vs file-edit duplicated UI and ephemeral-worker plumbing.
**Decision:** One File-Mode base on FE+BE drives all four (eventually five) subclasses; deltas live in subclass definitions only.
**Consequences:** (+) adding plan mode is a delta, not a parallel stack; (+) base bug-fixes reach all subclasses; (−) premature feature-specific constraints in the base must be resisted.
**Related invariants:** INV-9, INV-21.

### ADR-5: `paths.ba_home()` indirection via `BETTER_AGENT_HOME`
**Status:** Accepted (2026-05-13)
**Context:** Tests and dev scripts repeatedly clobbered the developer's real `~/.better-claude/` state.
**Decision:** All persistence routes through `paths.ba_home()`, which honors `BETTER_AGENT_HOME`, falls back to legacy `BETTER_CLAUDE_HOME`, and defaults to `~/.better-claude` with a local `~/.better-agent` alias when possible. Tests set the env var to a tempdir before importing backend.
**Consequences:** (+) tests are hermetic; (+) power users can relocate state; (−) new persistence sites must remember to call `ba_home()` instead of hardcoding.
**Related invariants:** INV-4, INV-20.

### ADR-6: No schema migrations — version bump + raise
**Status:** Accepted (2026-05-13)
**Context:** Migration code is high-cost, error-prone, and creates a long tail of compatibility shims.
**Decision:** Schema bumps raise on the old shape with a documented "wipe X" recovery; never silently migrate.
**Consequences:** (+) persistence code stays simple; (+) forces honest breaking-change communication; (−) wipe instructions must accompany every schema bump.
**Related invariants:** INV-6.

### ADR-7: Naming sweep — `claude_*` → `agent_*` on shared bases only
**Status:** Accepted (2026-05-13)
**Context:** The project is moving toward a generic "Better Agent" framing; shared base classes carry Claude-specific names that mislead readers.
**Decision:** Shared bases / abstract fields rename to `agent_*`. Concrete Claude classes / fields keep `claude_*`. Applied cross-cuttingly (sessions, events, tailers, providers, workers), not per-feature. Pending: FE `*_claude_session_id` → `*_agent_session_id` without renaming the repo directory.
**Consequences:** (+) adding a non-Claude provider doesn't require renames; (+) reads naturally as a base/concrete distinction; (−) in-progress rename produces transient asymmetry.
**Related invariants:** INV-7.

### ADR-8: In-process SDK MCP for the `delegate` tool
**Status:** Accepted (2026-05-13)
**Context:** Manager mode needs a `delegate` tool callable by `claude -p --print`, but `claude -p --print` ignores external (stdio/HTTP) MCP servers.
**Decision:** Register `delegate` via `create_sdk_mcp_server` (in-process). Never stdio/HTTP MCP for this tool.
**Consequences:** (+) manager-mode delegation works in `-p --print` flow; (+) zero MCP subprocess overhead; (−) tool must live inside the BC backend process.
**Related invariants:** INV-11.

### ADR-9: Event ordering — k-way merge with causal parent-before-child, single time axis for split panels
**Status:** Accepted (2026-05-13)
**Context:** Subagent JSONLs carry timestamps from when the worker actually ran, which is often earlier than the parent `tool_call` that spawned them in the parent stream. A flat `sorted(events, key=timestamp)` over the combined event set therefore renders the child before its parent — visually nonsensical and breaks the user's mental model. The same hazard exists across any pair of streams (supervisor verdict vs worker emission, rearranger update vs main agent, queue op vs assistant text). Supervisor mode additionally needs both panels to scroll on aligned time so causality between worker action and supervisor verdict is visible at a glance.
**Decision:**
1. **Per-stream order is law.** Within a single emitter (one entity_id / one source jsonl), event order is the order events arrived — never re-sorted by timestamp.
2. **Cross-stream interleaving is a k-way merge by timestamp**, using one cursor per stream and a min-heap on next-event timestamp.
3. **Causal override.** When a subagent event has a smaller timestamp than its parent `tool_call`, the parent timestamp wins for ordering purposes — children render after their parent regardless of clock skew.
4. **Supervisor split panel renders both panes into one shared vertical time axis.** Row height encodes elapsed time; idle stretches in one pane render as whitespace (never collapsed). Each pane still renders ONLY its native session (cross-mixin is forbidden — INV-13).
5. **Applies uniformly** to live ingestion, REST backfill projection, and WS replay (consistent with ADR-1).
**Consequences:** (+) ordering bugs become regression-testable with one synthetic timestamp-skew fixture; (+) supervisor causality is visually obvious; (+) consistent semantics across live and refresh views (no "looked right while streaming, scrambled on reload"); (−) more state in the ingestion projection (per-stream cursors, parent-uuid lookups); (−) split-panel rendering must track real-time alignment rather than per-pane independent layout, which is more layout work than two stacked lists.
**Related invariants:** INV-22, INV-23, INV-13, INV-15.

---

## 9. Out of Scope / Explicitly Rejected

- **OOS-1.** Schema migrations — pinky-swore against; old shape raises.
- **OOS-2.** Rewriting Claude's own session jsonl for rewind — use Claude CLI's native rewind APIs.
- **OOS-3.** Holding workers/sessions/projects in frontend state refreshed only by manual buttons. Stale data is wrong data.
- **OOS-4.** Auto-deny timeouts on pending approvals; the system waits indefinitely.
- **OOS-5.** Auto-spawning fresh workers without user approval in manager mode (after the shift to manual creation).
- **OOS-6.** Auto-spawning fresh workers at nested delegation depth.
- **OOS-7.** "Manager-tag everything" in supervisor mode — supervisor and worker are distinct entities, distinct tags, distinct panels.
- **OOS-8.** Storing ephemeral sessions (any File-Mode subclass) in the sidebar sessions list.
- **OOS-9.** Persisting orchestration-injected prompts as if they were user prompts.
- **OOS-10.** Auto-scrolling to bottom when the user has scrolled up.
- **OOS-11.** Fallback / silent error swallowing without explicit user approval.
- **OOS-12.** localStorage shadowing of state that also lives on the backend (other than pure UI prefs like panel widths/theme).
- **OOS-13.** Supervisor strategy inheriting from Manager strategy.
- **OOS-14.** Per-feature File-Mode bases (e.g. a prompt-engineering base separate from a file-edit base). One base, many subclasses.

---

## 10. Open Questions / Evolution Notes

### Open questions
- **OQ-1 Backfill heuristics vs deterministic msg_id.** `msg_id` at ingest supersedes `_group_events_into_turns`; migration in progress.
- **OQ-2 Subagent ordering.** k-way merge per-stream order vs flat timestamp sort; continuous tuning.
- **OQ-3 Manager-mode streaming content.** `update_running_content` was removed from `ManagerStrategy.apply_stream_event` at one point, leaving streaming text un-updated mid-turn. Pending verification.
- **OQ-4 Long file selections in comments.** Avoid embedding the whole selection while keeping clarity. Currently embeds full selection.
- **OQ-5 Frontend slowness** on session create/switch. Repeatedly flagged; no agreed root cause; progressive paging proposed.
- **OQ-6 Multi-host BC.** Backend per machine, one UI driving them; useful for cross-host manager/worker, dangerous because workers see different code state. Planning only.
- **OQ-7 Plan mode (4.1.5).** Referenced often; not yet implemented; must reuse the File-Mode base.
- **OQ-8 Image attachment loss** in engineering / forwarded prompts. Reproduced repeatedly; needs end-to-end attachment preservation across orch-injected messages.
- **OQ-9 Cancel race.** Queued events can be dropped on cancel; tail-F has no stderr capture. Low severity.
- **OQ-10 Two-Vite-dev-servers symptom.** HMR confusion when both :5173 listeners are alive simultaneously.
- **OQ-11 Tailer CPU spikes.** One report of tailers hitting 95% CPU; needs profiling.
- **OQ-12 Cross-frontier event bus / RxPython adoption.** Proposed direction; not yet adopted everywhere.
- **OQ-13 Worker init prompts.** A worker's first turn is an init-context prompt about its scope; a fork at that point becomes the reusable starting state. Not yet implemented.
- **OQ-14 Queued prompts not persisted to disk.** Backend restart loses unsent queued prompts. Direction: persist queue + replay on startup.
- **OQ-15 Online (live) ingestion broken — root cause located (2026-05-14 audit).** Was: "live view diverges from refresh view" (INV-15 / FR-ING.0.5 violation), user flagged twice on 2026-05-13. Root cause located by SPEC_DIVERGENCES.md DIV-1: REST `_reconcile_msg_events_from_jsonl` (backend/main.py:507) projects BOTH `msg_id`-tagged AND orphan events (no `msg_id`); WS `messages_replay` (backend/main.py:1948) uses `read_ws_events(msg_id_filter=msg_id)` which returns ONLY tagged events and silently drops orphans. Two different projections. User-confirmed (parallel session 2026-05-13T21:30: "events.jsonl is correct but the frontend isn't picking them up live"). Fix direction: unify projections — either include orphans in WS replay (matches REST), or backfill `msg_id` for all persisted events and drop the orphan branch in REST too.
- **OQ-16 Orch-injected prompts persisted as `type=user` in native JSONLs (broader pattern).** Empirical finding from `user_prompts.jsonl` sync (2026-05-13 / 2026-05-14): MULTIPLE classes of non-human input are persisted as `type=user` in native JSONLs and bleed through to downstream consumers — (a) supervisor's adversarial-review prompts (to supervisor session) and verdict-summary prompts (back to worker), (b) Claude Code skill auto-injection bodies (e.g. `Base directory for this skill: …`), (c) background-agent completion notifications (`<task-notification>…`). All three appear as `type=user` with no source-tag distinguishing them from human input. INV-14 is currently enforced only at render-time, not at persistence-time. Decide: keep relying on render-time tagging (and document the assumption), OR tag orch-injected prompts at write-time (distinct `type` or a sentinel `entrypoint` value) so consumers downstream of the JSONL can filter without prefix-matching prompt templates. **Bridge action taken:** `.claude/hooks/sync_user_prompts.py` now drops these three classes by prefix to keep `user_prompts.jsonl` clean as input for SPEC re-synthesis.
- **OQ-17 Supervisor prompt template iteration.** User has been actively shortening / refining the adversarial-supervisor template (2026-05-13: from a long "you are an adversarial supervisor… scrutinize against the original request…" form to a shorter "The worst cut: inventing answers the user owes…" form). The adversarial stance itself is locked (user confirmed "being adversarial is the right thing") — only the wording is in flux. Action: store the supervisor prompt template in one place (config or `prompt-eng/`) so iteration doesn't fork across mode strategies.
- **OQ-18 Supervisor as orthogonal summonable toggle (proposed reframe).** User direction (2026-05-13T22:37–22:44): supervisor stops being a sibling top-level orchestration mode (today's §4.2.3) and becomes an **orthogonal layer** that can be summoned on top of ANY existing session at any time, then disabled at will. Concrete details the user has committed to:
  1. **Summonable on any session.** From Native or Manager (and conceptually any future orchestration), the user can flip Supervisor ON mid-session. Once ON, supervisor supervises from that point onward — not retroactively over earlier turns.
  2. **Disable-able at will.** User can flip Supervisor OFF; supervisor work stops; supervisor's accumulated context is preserved on disk but its panel disappears from the UI.
  3. **Embedded / paired with the host session.** "isnt the supervisor session embedded in the worker session?" → yes. The supervisor session is paired internally to its host (like today's `supervisor_worker` is paired to a supervisor-mode session), not a free-standing top-level session.
  4. **In Manager mode, supervisor judges the manager, not the workers.** Explicit user clarification.
  5. **UI control = floating round toggle on the chat panel.** A round floating button with a supervisor icon, anchored to the top-right corner of the active session's chat panel (top-left in RTL). It is the SAME control whether supervisor is currently on or off — pressed state indicates "currently supervising".
  6. **Hidden panel when OFF.** When supervisor is disabled, the entire supervisor pane / split-panel UI MUST be hidden (no leftover empty pane, no shared-time-axis layout cost). When enabled, the split-panel chronology (FR-ORCH.3.9 / INV-23) reappears for the supervised period only.
  Decisions still pending: (a) does turning supervisor ON in mid-Manager-mode require a one-time approval like fresh-worker creation? (b) how is supervisor's "from this point onward" boundary persisted on disk so a backend restart resumes the right slice? (c) does this deprecate today's FR-ORCH.3.1 (supervisor as its own selectable orchestration mode at session-creation time), or coexist as a parallel entry point? Current §4.2.3 / INV-8 / ADR-3 still describe the as-implemented sibling-mode form — they MUST be revised once this reframe is accepted.
- **OQ-19 ClaudeProvider regression report (2026-05-13T21:52).** User reported "the claude provider doesnt work" in a fresh session (sid `0386e3db`); no further details captured in `user_prompts.jsonl`. Could be: OAuth refresh, CLAUDE_CONFIG_DIR resolution, model-list fetch, or runner spawn. Needs reproduction logs from that session's `events.jsonl` before any code-side action — do NOT guess at FR-PROV.* edits without evidence.

### Evolution notes
- Early prompts (April) focused on a `coder` subagent / git-worktree harness in a sibling project (`~/nns`) with hooks for wip-sync and checkpoint commits. Informed BC's persistence/recovery design but is not BC itself.
- Mid-April → early May: architecture shifted from an early "router / threads / distill" exploration to today's `native | manager | supervisor` set; the earlier modes are removed.
- Session-store-only persistence was superseded by a `SessionManager` single-writer; later, an `event_ingester` became the single-writer for `events.jsonl` with REST `_backfill_msgs` as the projection.
- Supervisor mode's own internal verdict work was originally rendered as plain "user" prompts (misleading). Current direction: ingest into the supervisor's own native session, render under a supervisor tag.
- The naming sweep (`claude_*` → `agent_*` on shared bases) is the most recent direction (May 13), driven by the move toward a generic "Better Agent" framing while keeping concrete provider names intact. Treated as cross-cutting, not per-feature.
