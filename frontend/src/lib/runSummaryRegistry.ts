// Singleton v2 run-summary store (ADR 0009, Package E frontend cutover).
//
// One `/ws/v2/surface` connection, subscribed to the non-cursor `runs`
// feed (`{"feeds": ["runs"]}` — adapter_api.py's `_FEED_SURFACES`),
// shared by every `useRunSummary`/`useRunSummaryByTurn` caller in the app
// — mirrors `sessionRegistry.ts`'s "one connection, many subscribers"
// shape for the session plane, just for runs. Cold-hydrates a session's
// run list on first subscribe (`GET /runs?session_id=`) and stays live
// via `run_summary_upsert` afterward; never polls.
//
// `run_id` is the correlation key back to the legacy `RunInfo.run_id`
// prop `RunBadge`/`ForkSplitView` already receive: both ultimately name
// the same on-disk `runs/<run_id>/` directory (`store_access.py`'s
// `RunRecord.run_id = run_dir.name`, and `turn_manager.run_state_add`'s
// `run_id` param is that same directory name threaded through from run
// creation) — so a v2 `RunSummaryWire` for a given `run_id` describes
// literally the same run a legacy `RunInfo` with that `run_id` does.

import { SurfaceClient } from "../adapter/client";
import type { RunsFrame, RunSummaryWire } from "../adapter/wire";
import { createFeedSocketRegistry } from "./surfaceFeedSocket";

type Listener = () => void;

/** Transport for the `runs` feed, built on the shared ref-counted factory
 * (`surfaceFeedSocket.ts`) like every other feed-only plane. This registry
 * never closes the connection once opened — it holds the ONE internal
 * `subscribeFrames` subscription below open forever (its unsubscribe is
 * never called), so `releaseSocketIfIdle` never sees zero listeners
 * regardless of how many `subscribeSession` callers come and go. */
const feedSocket = createFeedSocketRegistry<RunsFrame>(["runs"], (handlers, dispatch) => {
  handlers.onRunsFrame = dispatch;
});

class RunSummaryRegistry {
  private readonly client = new SurfaceClient();
  private opened = false;

  private byRunId = new Map<string, RunSummaryWire>();
  private runIdsBySession = new Map<string, Set<string>>();
  private hydratedSessions = new Set<string>();
  private inflightHydrate = new Map<string, Promise<void>>();
  private listenersBySession = new Map<string, Set<Listener>>();

  private ensureOpen(): void {
    if (this.opened) return;
    this.opened = true;
    feedSocket.subscribeFrames((frame) => this.applyUpsert(frame.summary));
  }

  private applyUpsert(summary: RunSummaryWire): void {
    this.byRunId.set(summary.run_id, summary);
    let ids = this.runIdsBySession.get(summary.session_id);
    if (!ids) {
      ids = new Set();
      this.runIdsBySession.set(summary.session_id, ids);
    }
    ids.add(summary.run_id);
    this.notify(summary.session_id);
  }

  private notify(sessionId: string): void {
    for (const cb of this.listenersBySession.get(sessionId) ?? []) cb();
  }

  private hydrate(sessionId: string): Promise<void> {
    if (this.hydratedSessions.has(sessionId)) return Promise.resolve();
    const inflight = this.inflightHydrate.get(sessionId);
    if (inflight) return inflight;
    const promise = this.client
      .fetchRuns(sessionId)
      .then((envelope) => {
        this.hydratedSessions.add(sessionId);
        if (envelope.kind !== "ok") return;
        for (const run of envelope.runs) this.applyUpsert(run);
        this.notify(sessionId);
      })
      .catch(() => {
        // Left un-hydrated: the next `subscribeSession` call (e.g. a
        // remount) retries. Live upserts for this session still apply
        // in the meantime — this only affects the COLD list.
      })
      .finally(() => {
        this.inflightHydrate.delete(sessionId);
      });
    this.inflightHydrate.set(sessionId, promise);
    return promise;
  }

  /** Subscribes to every v2 run-summary change for one session — opens
   * the shared connection (once, lazily) and cold-hydrates this session's
   * run list on first use. `cb` fires after hydration resolves and on
   * every subsequent live upsert touching this session; it does NOT fire
   * synchronously from this call (React callers re-render off their own
   * `useEffect`/`useState`, not this call's return). */
  subscribeSession(sessionId: string, cb: Listener): () => void {
    this.ensureOpen();
    let set = this.listenersBySession.get(sessionId);
    if (!set) {
      set = new Set();
      this.listenersBySession.set(sessionId, set);
    }
    set.add(cb);
    void this.hydrate(sessionId);
    return () => {
      set!.delete(cb);
      if (set!.size === 0) this.listenersBySession.delete(sessionId);
    };
  }

  /** Current snapshot for one run, or `undefined` when this registry has
   * never seen that `run_id` (not yet hydrated for its session, or a
   * `run_id` that predates this registry's live feed / has no v2 record
   * at all) — absence is never treated as an error. */
  getRun(runId: string): RunSummaryWire | undefined {
    return this.byRunId.get(runId);
  }

  /** The run currently anchored to `turnId` within `sessionId`, or
   * `undefined` when none is known — `useRunSummaryByTurn`'s lookup path
   * (B1/B3: the native run badge's kind label). `RunSummary.turn_id`
   * is the one correlation key content nodes need, since `NodeWire.run_ref`
   * is never populated by the current backend adapter (confirmed:
   * `adapters/normalize.py` has zero `Node(..., run_ref=...)` call sites)
   * — this session already has to be subscribed (via `subscribeSession`)
   * for this to see anything, same precondition `getRun` has. */
  getRunByTurnId(sessionId: string, turnId: string): RunSummaryWire | undefined {
    const ids = this.runIdsBySession.get(sessionId);
    if (!ids) return undefined;
    for (const id of ids) {
      const run = this.byRunId.get(id);
      if (run?.turn_id === turnId) return run;
    }
    return undefined;
  }
}

export const runSummaryRegistry = new RunSummaryRegistry();
