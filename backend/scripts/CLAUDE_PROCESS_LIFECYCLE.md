# Claude Code CLI Process Lifecycle — Empirical Report

**Date:** 2026-06-11
**Claude Code version:** 2.1.165
**Platform:** macOS (Darwin 24.5.0, arm64)
**Test file:** `backend/scripts/test_claude_binary_process_lifecycle.py`

> **Platform note:** All tests ran on macOS. Orphan reparenting (ppid→1) is
> launchd-specific. Linux uses init, Windows has different semantics entirely.
> BC's `proc_control.py` has platform-specific paths for both.

---

## Context

**Better Agent (BC)** is a web UI that orchestrates Claude Code CLI sessions.
BC's backend (Python/FastAPI) spawns `claude` processes as subprocesses via the
`claude_agent_sdk` Python package. Each claude process handles one conversation
(BC calls these "sessions"). A user interacting with the BC web UI is driving
these claude subprocesses indirectly.

**TestApe** is a testing framework that binds test sessions to the claude
process's PID (`$PPID`). Understanding the claude process lifecycle is critical
for TestApe — if the process dies unexpectedly, test sessions leak.

The **native session file** is the claude CLI's own conversation log at
`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`. BC tails this file to
receive streaming events. Its mtime (last-modified time) is a signal for
whether the claude process is still writing.

---

## Process Hierarchy: BC Backend → Runner → Claude

Each turn spawns a hierarchy of three processes:

```
BC backend (Python FastAPI, long-lived)
  └─ runner.py subprocess (per-run, detached)
       └─ claude CLI process (per-run, spawned by SDK)
```

**Who owns what:**

| Layer | File | Owns | Spawns | Lifetime |
|-------|------|------|--------|----------|
| **Backend** | `orchestrator.py` | Sessions, providers, WS, queues | runner via provider | Process-lives forever |
| **TurnManager** | `turn_manager.py` | Turn lifecycle (`lifecycle.turn_*` events), active run IDs, cancel events, run state | Nothing — delegates to provider | Per-coordinator, in-process |
| **Provider** | `provider_claude.py` | Runner subprocess (`popen`), run state, cancel/tail watches | `runner.py` via `subprocess.Popen` | Per-run, detached |
| **Runner** | `runner.py` | SDK client, claude process, turn I/O, heartbeat, babysitter linger | `claude` CLI via SDK `ClaudeSDKClient` | One turn, then exits — unless background work is alive (babysitter linger) |
| **Claude CLI** | external binary | Session state, tool calls, bg shells, monitors | Tool child processes | Until stdin closes or killed |

**The flow for a single turn:**

1. Frontend sends prompt → backend receives via REST
2. `Orchestrator.run_turn()` delegates to `TurnManager.run_turn()`
3. `TurnManager` calls `provider.start_run()` which spawns `runner.py` as a detached subprocess
4. `runner.py` connects `ClaudeSDKClient`, sends the prompt, streams events back via a queue
5. `TurnManager` drains the queue in a loop, feeding events to the WS callback and `apply_event`
6. On completion, `TurnManager` emits `lifecycle.turn_complete` on the event bus
7. `TurnManager.run_state_remove()` cleans up the run ID

**The `runner.py` process is the claude subprocess's parent.** The runner:
- Spawns claude via the SDK (which uses `SubprocessCLITransport`)
- Writes `state.json` (discovered session ID, jsonl path) for the backend to tail
- Writes `complete.json` when done
- Sends heartbeat (`runner_alive`) every 5s so the backend can detect stuck runners

**Every run serves exactly one turn.** Session continuity comes from
`--resume <agent_sid>` — the API prompt cache is content-prefix-based, so a
fresh process pays no extra cache cost over a kept-alive one.

---

## Per-Turn Spawn + Babysitter Linger

Every user turn spawns a fresh `runner.py` → claude process. At the
`complete` event the runner writes the run-level `complete.json`
(turn finalized from the backend's perspective) and then decides:

```
complete.json written
  └─ has_detached_descendants(self, ignore_pgids=<runner's own spawns>) ?
       ├─ False → disconnect SDK → claude exits → runner exits
       └─ True  → BABYSITTER LINGER:
                    keep the SDK connection open (claude + its bg shells
                    and Monitor watchers stay alive), heartbeat keeps
                    refreshing runner_alive, poll the signal every 2s.
                    NEVER sends another prompt to this instance.
                    ├─ signal drops (bg work ended) → disconnect → exit
                    └─ run_dir/cancel appears → sweep detached groups → exit
```

New prompts ALWAYS spawn a fresh `--resume` instance — even while an old
instance lingers. This is safe because a lingering instance can never run
a turn: its stdin is silent and the in-process timer tools (CronCreate /
CronDelete / CronList / ScheduleWakeup) are stripped on every spawn via
`disallowed_tools` (T17), so nothing can wake it. The lingering instance
therefore never writes to the shared session jsonl (locked by
`test_babysitter_linger.py`), which matters because `--resume` CONTINUES
the same session id and APPENDS to the same jsonl file (T16).

**The reap signal** is `proc_control.has_detached_descendants` (T14/T15):
a live ppid-descendant in a different process group = a `run_in_background`
shell or Monitor watcher. Same-group descendants (CLI, MCP servers,
foreground tools) never count; the runner's own deliberate service spawns
(canvas auto-start) are excluded via `ignore_pgids`.

**Timers are backend-owned now.** The runner attaches a `scheduler`
in-process MCP server (`schedule_create` / `schedule_list` /
`schedule_delete`); the handlers POST to `/api/internal/schedules`, the
backend persists schedules durably (`stores/schedule_store.py`) and a
global ticker (`scheduler.py`) fires each due schedule as a normal prompt
through `coordinator.submit_prompt` (`source="schedule"`,
`user_initiated=False`). Unlike the old in-process timers, schedules
survive crashes and restarts.

**Provider-side handling** (`provider_claude._watch_complete`): the turn
finalizes when complete.json APPEARS — not when the process exits — so a
lingering runner doesn't block turn completion. The run then stays
registered with `lingering=True` (so cancel/kill levers still resolve it,
surfaced via the `run_lingering` WS event and
`GET /api/sessions/{sid}/background`), and the jsonl tailer keeps running
until the process exits (late post-Result CLI flushes still flow —
`test_linger_late_flush.py`). `_watch_linger_exit` deregisters the run
when the process finally dies.

**Cancel paths:**

- **Mid-turn stop**: `cancel_turn()` → `run_dir/cancel` → runner's
  `_cancel_watcher` → `client.interrupt()` → drain → runner sweeps its own
  setsid'd bg shells → complete.json → exit. No backend killpg.
- **Kill background work** (during a linger): same sentinel — the linger
  loop sees it, sweeps the detached groups, exits.
  (`POST /api/sessions/{sid}/background/kill`.)
- **Hard kill** (session delete / shutdown Y=kill): `cancel_run()` —
  killpg + detached-group sweep.

On backend restart, a lingering runner is classified `already_complete`
(complete.json exists) by `run_recovery` — events replayed, process left
alone; it self-reaps when its background work ends.

---

## How BC Spawns Claude

BC uses `ClaudeSDKClient` (from `claude_agent_sdk`) which launches claude via
`SubprocessCLITransport._build_command()`. The base command includes:

```
claude --output-format stream-json --verbose --input-format stream-json \
       --permission-mode bypassPermissions
```

BC's runner adds `--model`, `--cwd` (via env), `--resume`, `--mcp-config`,
`--system-prompt` (preset + append), `--disallowed-tools`, `--setting-sources`,
and `--plugin-dir` depending on configuration. No `-p` flag — the process reads
JSON from stdin, writes JSON to stdout.

### BC's Runner Mode

| Flow | Process lifetime |
|------|-----------------|
| connect → `_run_one_turn` → complete.json → (babysitter linger iff bg work) → disconnect → exit | One turn per process; lingers only while detached background work lives |

Test T1 mirrors the per-turn path; T2 documents the raw binary's
multi-turn capability over one stdin (which BC no longer uses).

### SDK Shutdown Sequence

When BC disconnects, the SDK's `close()` method executes:

1. Close stdin (EOF)
2. Wait up to **5 seconds** for process to exit gracefully
3. If timeout: send **SIGTERM**
4. Wait another **5 seconds**
5. If still alive: send **SIGKILL**

This 3-stage sequence means "stdin close" alone doesn't kill the process if
it's stuck in a tool call (like Monitor). The process has up to 10s before
forced termination.

---

## Core Lifecycle Facts

| Fact | Proven by | Detail |
|------|-----------|--------|
| Process exits on stdin close | T1 | SDK `close()` closes stdin, process exits |
| Same PID across multiple turns | T2 | 3 turns, 5s idle gap, same PID |
| SIGTERM kills the process | T4 | rc=143, no respawn |
| SIGKILL kills the process instantly | T9 | rc=-9 on macOS (signal number; platform-specific) |
| Resume creates a NEW process | T10 | Different PID, old PID dead |
| Process is NOT a daemon | T4 | No cron, no respawn, no supervisor |
| Process does NOT auto-reap on idle | T2 | Stays alive indefinitely while stdin open |
| `has_detached_descendants` tracks bg shells | T14 | False before, True during, drops after — from both claude-pid and runner-pid positions |
| Monitor watchers are process-tree-visible | T15 | setsid'd into their own group; the reap signal sees them |
| `--resume` while the original lives: same sid, same jsonl, APPENDS | T16 | the two instances are concurrent writers to one file — hence the "lingering instance never runs a turn" rule |
| `--disallowedTools` genuinely strips the timer tools | T17 | model never emits a timer tool_use; reports them unavailable |

---

## Background Operation Coverage

### 1. `Bash run_in_background=true` (declared bg shell)

BC tracks these shells via the `run_in_background` tool parameter. Their parent
chain traces back to the claude process (`claude → zsh → bash`).

| Termination method | Shell fate | Test |
|--------------------|------------|------|
| Disconnect (stdin close) | **Gone** | T5 |
| SIGTERM | **Gone** | T7 |
| SIGKILL | **Still alive** (orphaned) | T9 |

The exact termination mechanism on disconnect/SIGTERM is not verified — could
be process-group signal, SIGHUP, or SDK cleanup. What's proven: the shell is
absent from the process table after SIGTERM/disconnect, present after SIGKILL.

### 2. Undeclared daemons (`nohup &`, `setsid`, `&`)

BC does NOT track these. They are reparented to launchd (ppid=1) on macOS
immediately. They survive all termination methods.

| Termination method | Daemon fate | Test |
|--------------------|-------------|------|
| Disconnect (stdin close) | **Orphaned** (ppid=1) | T3, T6 |
| SIGTERM | **Orphaned** (ppid=1) | T8 |
| SIGKILL | **Orphaned** (ppid=1) | T9 |

### 3. Monitor — THE ONLY OPERATION THAT SURVIVES STDIN CLOSE

When a Monitor is active, the claude process stays alive for at least 10 seconds
past stdin close (T12). This is fundamentally different from all other operations.
The process likely survives until the monitor completes or the SDK's 5s+5s grace
period expires and force-kills it. The watcher process is setsid'd into its
own group (T15), so the babysitter reap signal keeps the runner alive while
a Monitor runs.

| Termination method | Monitor fate | Test |
|--------------------|-------------|------|
| Disconnect (stdin close) | **Process stays alive ≥10s** | T12 |

### 4. CronCreate — DISALLOWED in BC

Raw binary behavior: fires a new turn on the same process (same PID) while
alive; on disconnect the process exits and the timer is lost. **BC strips
this tool on every spawn** (T17) and replaces it with the backend-owned
durable scheduler — a lingering babysitter must never run a turn of its
own (T16).

| Fact | Test |
|------|------|
| Cron fires on same process (same PID) | T11 |
| Process exits on disconnect despite active cron | T11 |

### 5. ScheduleWakeup — DISALLOWED in BC

Raw binary behavior: like CronCreate — fires while the process is alive,
does NOT keep it alive past stdin close. **BC strips it too** (same
scheduler replacement).

| Fact | Test |
|------|------|
| Does NOT keep process alive past stdin close | T13 |
| Fast exit (<5s) on disconnect | T13 |

### 6. Agent tool (in-process)

Does NOT spawn a new OS process. Runs entirely in-process. Verified empirically:
no new claude PIDs appear during Agent tool use. No lifecycle impact.

### 7. Workflow tool (in-process)

Does NOT spawn new OS processes. Sub-agents run in-process. Verified empirically:
no new claude PIDs appear during Workflow use. No lifecycle impact.

---

## SIGTERM vs SIGKILL: Critical Difference

| | SIGTERM | SIGKILL |
|---|---|---|
| Declared bg shell | Gone (mechanism unverified) | **Still alive** (orphaned) |
| Undeclared daemon | Orphaned | Orphaned |
| In-memory timers | Lost | Lost |

BC's `proc_control.py` uses SIGTERM first (`graceful_stop`), then SIGKILL after
timeout. Declared bg shells survive SIGKILL but not SIGTERM — the graceful path
gives the CLI time to clean them up.

---

## Resume: New Process

When BC resumes a session (`--resume <session_id>`), a **brand new** claude
process is spawned. The old PID is dead. The new process reconnects to the
existing session state (CLI reads its own session files from
`~/.claude/projects/<encoded-cwd>/<sid>.jsonl`).

Note: `--resume <sid>` (explicit session) is different from `--continue`
(most recent session). BC uses `--resume`.

**T16 (resume-while-alive):** `--resume` CONTINUES the same session id and
APPENDS to the same jsonl file — even while the original instance is still
alive. Two live instances on one session are concurrent writers to one
file (lines stay valid JSON, but histories interleave). BC's babysitter
therefore never lets a lingering instance run a turn.

---

## Measured Cost of Per-Turn Spawn (2026-06-11, glm-5.1 via configured base URL — locked by T18)

Spawn latency: spawn → ready 0.6s, spawn → result (trivial turn) ~2s.

Cross-process cache behavior is **INTERMITTENT** — three same-day
measurements of a `--resume` turn in a NEW process (vs. the in-process
turn-2 baseline that always reads its prefix from cache):

| Run | In-process turn 2 (read / created) | Resumed new process (read / created) | Verdict |
|-----|-----------------------------------|--------------------------------------|---------|
| A | 29,514 / 4,094 | 0 / 33,598 | zero hit |
| B | 29,395 / 4,092 | 14,831 / 19,794 | partial (static prefix only) |
| C | 33,487 / 1,125 | 34,625 / 290 | full hit |

**Finding:** prefix identity across spawns sometimes survives (full
cache hit, run C) and sometimes diverges at per-spawn dynamic content
(partial/zero, runs B/A). The per-turn-spawn cache cost is therefore
bounded by ONE prefix re-creation per turn in the worst case and zero
in the best — not a systematic regression. T18 asserts the
deterministic invariant (in-process caching works) and prints the
cross-boundary measurement on every run; keep this table in sync if its
verdict distribution shifts. Re-measure on Anthropic's API before
treating these numbers as universal.

---

## Native Session File mtime vs Process Exit

The claude CLI writes events to its session jsonl continuously during a turn.
A natural question: **can you use the session file's mtime to detect whether
the claude process is still alive?**

Empirical results (8 runs: 5 graceful disconnect, 3 SIGKILL):

| Method | Runs | mtime < exit time? | Diff range | Last line valid JSON? |
|--------|------|--------------------|------------|-----------------------|
| Disconnect (stdin close) | 5 | ✅ Always | 0.42–0.63s | ✅ Yes |
| SIGKILL | 3 | ✅ Always | 1.91–1.92s | ✅ Yes |

**Key findings:**

1. **mtime is always before process exit** — across all 8 runs, the session
   file's last write happened before the process died. This means watching
   mtime gives you a reliable "last activity" signal.

2. **Even SIGKILL doesn't corrupt the jsonl** — the last line is always valid
   JSON. The CLI flushes each line before the next event, so SIGKILL never
   catches it mid-write to a line. (It may lose the *next* event that was
   being composed, but never truncates an existing line.)

3. **The gap is ~0.5s for graceful, ~1.9s for SIGKILL** — the SIGKILL gap
   is larger because the jsonl was written before the `sleep(1)` delay before
   the kill. The actual write-to-death latency under SIGKILL is near-instant.

**Caveat:** These tests used a simple single-turn conversation. A turn that's
mid-stream (claude actively streaming tokens) when killed may have a different
mtime pattern. The jsonl is line-buffered, so partially-written lines shouldn't
occur, but this wasn't tested with concurrent writes at the exact moment of kill.

### How TurnManager Knows When Turns Start and End

TurnManager fires both `turn_start` and `turn_complete`/`turn_stopped`. It
doesn't detect these from outside — it **creates** them by owning the drain
loop that waits for the runner to finish.

**`turn_start`** — fired at `turn_manager.py:744`, immediately *before* calling
`_drive_cli_run()`. TurnManager knows the turn is starting because it's the one
starting it. No signal from claude needed.

**`turn_complete`/`turn_stopped`** — TurnManager knows the turn ended because
`_drive_cli_run()` **returns**. It runs a drain loop:

```
while True:
    event = await queue.get()       # blocks until runner posts
    ws_callback(event)              # apply_event + broadcast
    if event.type == "complete":    # terminal from runner
        break
    if cancel_event.is_set():       # user hit stop
        break
```

The runner (`runner.py`) calls `client.query(prompt)`, which streams events from
claude via the SDK. When claude finishes the turn, the SDK delivers a final
`complete` event. The runner posts it to the queue. The drain loop sees it,
breaks, returns to `run_turn()`, and TurnManager fires the appropriate terminal:

| Terminal path | Event emitted | Location |
|---------------|--------------|----------|
| Success | `turn_complete` + bus `lifecycle.turn_complete` | `:839`, `:876` |
| Cancel | `turn_stopped` + bus `lifecycle.turn_stopped` | `:909`, `:918` |
| Error | `turn_complete(success=False)` + bus `lifecycle.turn_stopped` | `:1056`, `:974` |
| Backend shutdown (CancelledError) | `turn_detached` (no lifecycle emit) | `:936` |

The `turn_detached` case is special — it means the backend's own task was
cancelled (restart/shutdown), but the runner and claude are likely still alive.
A fresh backend picks them up via `run_recovery`.

---

## Implications for TestApe Binding

1. **PID binding tracks the current process** but must re-bind on resume (T10:
   new PID). A stale PID means a dead process.

2. **Undeclared daemons leak forever** — no mechanism in claude or BC cleans
   them up. They get ppid=1 and run indefinitely. TestApe needs its own orphan
   detection if it cares about these.

3. **Monitor is a special case** — the claude process can outlive a BC
   disconnect by ≥10s when a Monitor is active (T12). TestApe must not assume
   the process died immediately on disconnect.

4. **SIGKILL leaves declared bg shells orphaned** — BC's crash recovery handles
   session state reconciliation, but orphaned shells are NOT cleaned up.
   BC's `proc_control.py` has `kill_detached_descendant_groups` for this, but
   it only runs during explicit stop, not crash recovery.

5. **Session file mtime is a reliable liveness signal** — the jsonl mtime is
   always before process exit. If mtime stopped advancing and the process PID
   is dead, the session is truly finished. This can be used as a fallback when
   PID polling isn't possible (e.g. from a different machine).
