/** Single source of truth for "is this session running" + "how many
 * unseen events does it have" on the frontend, AND for the per-project
 * aggregate derived from those.
 *
 * Architecture (per CLAUDE.md state-ownership rule):
 *
 *   • Backend owns per-session state: `is_running`, `unread_count`,
 *     `cwd`, `node_id`. We snapshot from `GET /api/sessions` at
 *     bootstrap, then apply WS deltas.
 *
 *   • Per-project aggregates are PURE DERIVATIONS from the per-session
 *     state — we derive locally instead of re-fetching `/api/projects`.
 *     This is what eliminates the `/api/projects` refetch storm: the
 *     old design called `refreshProjects()` on every WS
 *     `projects_changed` ping (which the backend fanned out on every
 *     running/unread/seen change). Under load that was ~132
 *     `/api/projects` calls/min.
 *
 *   • Sessions with `cwd === ""` in the WS payload are sidebar-hidden
 *     (`working_mode` set — file_editing / engineering / etc.). Their
 *     per-session state is still tracked (the chat view consumes it),
 *     but they DO NOT contribute to any aggregate. This mirrors the
 *     backend's `should_hide_from_sidebar` filter applied in
 *     `_project_aggregates` (main.py:761).
 *
 *   • `projects_changed` WS event is reserved for STRUCTURAL list
 *     changes (project add / delete / touch). The backend's
 *     broadcaster no longer fires it as a side-effect of running /
 *     unread / seen changes. `App.tsx` still listens and refetches
 *     `/api/projects` for the project METADATA list (path / name); the
 *     registry no longer touches it.
 *
 *   • Subscriptions flow through the typed eventBus. Per-sid /
 *     per-project listener sets gate React re-renders so an unread
 *     bump on session A doesn't force every `<SessionStatusBadge>`
 *     to re-render.
 *
 * Bootstrap sequencing:
 *   1. `bind()` wires bus subscriptions. Until `_bootstrapped === true`
 *      they BUFFER deltas into `_deltaBuffer` rather than apply.
 *   2. `bootstrap()` fetches `/api/sessions` once, builds the sessions
 *      map + derived projects map, drains the buffer in arrival order,
 *      then flips `_bootstrapped = true`.
 *   3. Concurrent `bootstrap()` calls (e.g. mount + visibilitychange
 *      racing) share the same in-flight promise.
 *   4. A bootstrap that fails (network) leaves `_bootstrapped = false`
 *      and continues buffering; the buffer survives across retries.
 *      Successful re-bootstrap (e.g. on visibilitychange) AFTER the
 *      flag is already true bypasses the buffer-drain path (deltas are
 *      already applying directly).
 *
 * Two consumer hooks:
 *   • `useSessionMeta(sid)` — `{ is_running, unread_count }` for one sid.
 *   • `useProjectAggregate(path, nodeId)` — `{ running_count,
 *     unread_session_count }` for one project (matched by cwd + node_id).
 */

import { useSyncExternalStore } from "react";

import { API } from "../api";
import { subscribeMany } from "./eventBus";

export type MonitoringState =
  | "active"
  | "idle"
  | "blocked_on_user"
  | "waiting_on_background"
  | "stopped";

export interface MarkerInfo {
  color: string;
  tooltip: string;
  sound?: boolean;
  // Source tag (e.g. NEEDS_USER_DECISION / ALL_TASKS__DONE). Set by the
  // backend at marker-detect time so status sort classifies by tag, never
  // by drifting color/tooltip.
  tag?: string;
}

export interface SessionMeta {
  is_running: boolean;
  unread_count: number;
  monitoring_state: MonitoringState;
  markers: Record<string, MarkerInfo>;
  testape_active?: boolean;
}

export interface ProjectAggregate {
  running_count: number;
  unread_session_count: number;
}

// Internal per-session record. `monitoring_state` is the SINGLE source of
// session state — `is_running` is the derived projection
// `monitoring_state !== "stopped"`, computed in `getSession`/aggregates, never
// stored. Carries `(cwd, node_id)` to route deltas to the right project
// aggregate; NOT exposed to consumers — `useSessionMeta` projects to
// `SessionMeta`.
interface SessionEntry {
  unread_count: number;
  monitoring_state: MonitoringState;
  cwd: string;
  node_id: string;
  markers: Record<string, MarkerInfo>;
  testape_active?: boolean;
}

/** The one place `is_running` is defined: a session is running iff its
 * monitoring state is anything other than "stopped". */
function isRunning(state: MonitoringState): boolean {
  return state !== "stopped";
}

const EMPTY_MARKERS: Record<string, MarkerInfo> = {};
const EMPTY_SESSION: SessionMeta = {
  is_running: false,
  unread_count: 0,
  monitoring_state: "stopped",
  markers: EMPTY_MARKERS,
  testape_active: false,
};
const EMPTY_AGGREGATE: ProjectAggregate = {
  running_count: 0,
  unread_session_count: 0,
};

type Listener = () => void;
type BufferedDelta =
  | { type: "session_running_changed"; payload: SessionRunningPayload }
  | { type: "session_monitoring_changed"; payload: SessionMonitoringPayload }
  | { type: "run_state"; payload: RunStatePayload }
  | { type: "session_unread_changed"; payload: SessionUnreadPayload }
  | { type: "session_marker_changed"; payload: SessionMarkerPayload }
  | { type: "session_created"; payload: SessionCreatedPayload }
  | { type: "session_deleted"; payload: SessionDeletedPayload }
  | { type: "session_metadata_updated"; payload: SessionMetadataPayload }
  | { type: "testape_session_state"; payload: { session_id: string; active: boolean } };

interface SessionRunningPayload {
  session_id: string;
  value: boolean;
  cwd?: string;
  node_id?: string;
}
// Carries (cwd, node_id) so it can route the project aggregate +
// materialize a not-yet-seen session.
interface SessionMonitoringPayload {
  session_id: string;
  monitoring_state: MonitoringState;
  cwd?: string;
  node_id?: string;
}
interface RunStatePayload {
  app_session_id: string;
  runs: unknown[];
}
interface SessionUnreadPayload {
  session_id: string;
  unread_count: number;
  cwd?: string;
  node_id?: string;
}
interface SessionMarkerPayload {
  session_id: string;
  extension_id: string;
  marker: MarkerInfo | null;
}
interface SessionCreatedPayload {
  session: {
    id: string;
    cwd?: string;
    node_id?: string;
    is_running?: boolean;
    unread_count?: number;
  };
}
interface SessionDeletedPayload {
  session_id?: string;
}
interface SessionMetadataPayload {
  session_id: string;
  patch?: {
    cwd?: string;
    node_id?: string;
  };
}

class SessionRegistry {
  // Per-sid entry — populated by bootstrap REST + WS deltas.
  // Sessions enter the map via THREE paths only: bootstrap mass
  // insert, `session_created`, or `session_metadata_updated`. The
  // running/unread patch handlers REFUSE to insert (guards against
  // phantom entries from delta events for sessions that should have
  // been filtered server-side).
  private sessions: Map<string, SessionEntry> = new Map();

  // Per-project aggregate keyed by `<node_id>::<cwd>`. Derived from
  // `sessions` by `recomputeProject`; never authoritative on its own.
  private projects: Map<string, ProjectAggregate> = new Map();

  private version = 0;

  private sessionListeners: Map<string, Set<Listener>> = new Map();
  private projectListeners: Map<string, Set<Listener>> = new Map();

  private busUnsub: (() => void) | null = null;
  private domUnsub: (() => void) | null = null;

  // Bootstrap state machine. `_bootstrapped` flips ONLY after a
  // successful bootstrap; a network-failed bootstrap leaves it false
  // so deltas continue to buffer until the next attempt succeeds.
  private _bootstrapped = false;
  private _bootstrapInFlight: Promise<void> | null = null;
  private _deltaBuffer: BufferedDelta[] = [];

  /** Wire bus subscriptions + DOM lifecycle. Idempotent — calling
   * twice detaches the prior wire-up first. */
  bind() {
    if (this.busUnsub) this.busUnsub();
    if (this.domUnsub) this.domUnsub();

    this.busUnsub = subscribeMany([
      ["session_running_changed", (p) => {
        this.dispatch("session_running_changed", p as SessionRunningPayload);
      }],
      ["session_monitoring_changed", (p) => {
        this.dispatch("session_monitoring_changed", p as SessionMonitoringPayload);
      }],
      ["run_state", (p) => {
        this.dispatch("run_state", p as RunStatePayload);
      }],
      ["session_unread_changed", (p) => {
        this.dispatch("session_unread_changed", p as SessionUnreadPayload);
      }],
      ["session_marker_changed", (p) => {
        this.dispatch("session_marker_changed", p as SessionMarkerPayload);
      }],
      ["session_created", (p) => {
        this.dispatch("session_created", p as SessionCreatedPayload);
      }],
      ["session_deleted", (p) => {
        this.dispatch("session_deleted", p as SessionDeletedPayload);
      }],
      ["session_metadata_updated", (p) => {
        this.dispatch("session_metadata_updated", p as SessionMetadataPayload);
      }],
      ["testape_session_state", (p) => {
        this.dispatch("testape_session_state", p as { session_id: string; active: boolean });
      }],
    ]);

    // Drift recovery: when the tab comes back into focus, re-snapshot.
    // With the single-REST bootstrap this is one `/api/sessions` call.
    // Same handler covers both `visibilitychange` (becoming visible)
    // and explicit `focus` (some browsers fire one but not the other).
    const onResume = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      void this.bootstrap();
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onResume);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("focus", onResume);
    }
    this.domUnsub = () => {
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onResume);
      }
      if (typeof window !== "undefined") {
        window.removeEventListener("focus", onResume);
      }
    };
  }

  /** Bootstrap from `/api/sessions`. Aggregates are derived locally
   * by summing visible sessions — no `/api/projects` call. Concurrent
   * callers await the same in-flight promise. */
  bootstrap(): Promise<void> {
    if (this._bootstrapInFlight) return this._bootstrapInFlight;
    this._bootstrapInFlight = this._doBootstrap().finally(() => {
      this._bootstrapInFlight = null;
    });
    return this._bootstrapInFlight;
  }

  private async _doBootstrap(): Promise<void> {
    let res: Response;
    try {
      res = await fetch(`${API}/api/sessions`);
    } catch {
      // Network failure — keep buffering, leave `_bootstrapped` as-is.
      return;
    }
    if (!res.ok) return;
    let json: unknown;
    try {
      json = await res.json();
    } catch {
      return;
    }
    const rows = (json && typeof json === "object" && "sessions" in json
      ? (json as { sessions: unknown }).sessions
      : []) as Array<{
      id?: string;
      is_running?: boolean;
      unread_count?: number;
      cwd?: string;
      node_id?: string;
      monitoring_state?: string;
      markers?: Record<string, MarkerInfo>;
    }>;

    const nextSessions = new Map<string, SessionEntry>();
    for (const s of rows) {
      if (!s?.id) continue;
      // Prefer the explicit monitoring_state; fall back to the legacy
      // is_running boolean (active/stopped) if an older backend omits it.
      const ms: MonitoringState = (s.monitoring_state as MonitoringState)
        || (s.is_running ? "active" : "stopped");
      nextSessions.set(s.id, {
        unread_count: Math.max(0, Number(s.unread_count) || 0),
        monitoring_state: ms,
        cwd: s.cwd ?? "",
        node_id: s.node_id || "primary",
        markers: (s.markers && typeof s.markers === "object") ? s.markers : {},
        testape_active: false,
      });
    }
    this.sessions = nextSessions;
    this.projects = this.deriveAllProjects(nextSessions);
    this.version += 1;

    // First successful bootstrap — drain anything buffered while the
    // bus was bound but the snapshot hadn't arrived yet. Apply in
    // arrival order so the final state matches the order the backend
    // emitted the events. Subsequent bootstraps (visibilitychange)
    // skip this — deltas were already applying directly.
    if (!this._bootstrapped) {
      const buf = this._deltaBuffer;
      this._deltaBuffer = [];
      this._bootstrapped = true;
      for (const ev of buf) this.applyDelta(ev);
    }

    this.notifyAll();
  }

  // ── Bus delta routing ────────────────────────────────────────────

  private dispatch<T extends BufferedDelta["type"]>(
    type: T,
    payload: Extract<BufferedDelta, { type: T }>["payload"],
  ) {
    if (!payload) return;
    if (!this._bootstrapped) {
      this._deltaBuffer.push({ type, payload } as BufferedDelta);
      return;
    }
    this.applyDelta({ type, payload } as BufferedDelta);
  }

  private applyDelta(ev: BufferedDelta) {
    switch (ev.type) {
      case "session_running_changed":
        return this.onRunning(ev.payload);
      case "session_monitoring_changed":
        return this.onMonitoring(ev.payload);
      case "run_state":
        return this.onRunState(ev.payload);
      case "session_unread_changed":
        return this.onUnread(ev.payload);
      case "session_marker_changed":
        return this.onMarker(ev.payload);
      case "session_created":
        return this.onCreated(ev.payload);
      case "session_deleted":
        return this.onDeleted(ev.payload);
      case "session_metadata_updated":
        return this.onMetadataUpdated(ev.payload);
      case "testape_session_state":
        return this.onTestApeState(ev.payload);
    }
  }

  // ── Per-event handlers ───────────────────────────────────────────

  private onRunning(d: SessionRunningPayload) {
    if (!d.session_id) return;
    this.applyRoutedDelta(d.session_id, d.cwd ?? "", d.node_id ?? "primary", {
      monitoring_state: d.value ? "active" : "stopped",
    });
  }

  private onMonitoring(d: SessionMonitoringPayload) {
    if (!d.session_id) return;
    this.applyRoutedDelta(d.session_id, d.cwd ?? "", d.node_id ?? "primary", {
      monitoring_state: d.monitoring_state,
    });
  }

  private onRunState(d: RunStatePayload) {
    if (!d.app_session_id || !Array.isArray(d.runs)) return;
    const prev = this.sessions.get(d.app_session_id);
    if (!prev) return;
    this.applyRoutedDelta(d.app_session_id, prev.cwd, prev.node_id, {
      monitoring_state: d.runs.length > 0 ? "active" : "stopped",
    });
  }

  private onUnread(d: SessionUnreadPayload) {
    if (!d.session_id) return;
    this.applyRoutedDelta(d.session_id, d.cwd ?? "", d.node_id ?? "primary", {
      unread_count: Math.max(0, Number(d.unread_count) || 0),
    });
  }

  private onMarker(d: SessionMarkerPayload) {
    if (!d.session_id || !d.extension_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return; // markers don't materialize a session
    const markers = { ...prev.markers };
    if (d.marker) {
      markers[d.extension_id] = {
        color: d.marker.color,
        tooltip: d.marker.tooltip,
        ...(d.marker.sound ? { sound: true } : {}),
        ...(d.marker.tag ? { tag: d.marker.tag } : {}),
      };
    } else {
      delete markers[d.extension_id];
    }
    this.sessions.set(d.session_id, { ...prev, markers });
    this.version += 1;
    this.notifySession(d.session_id);
  }

  private onTestApeState(d: { session_id: string; active: boolean }) {
    if (!d.session_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return;
    this.sessions.set(d.session_id, {
      ...prev,
      testape_active: d.active,
    });
    this.version += 1;
    this.notifySession(d.session_id);
  }

  /** Shared delta-apply path for monitoring-state + unread. The
   * (cwd, node_id) pair in the WS payload carries the backend's per-call
   * `should_hide_from_sidebar` verdict (empty cwd = hidden). Treating
   * it as the authoritative routing key — instead of trusting a
   * possibly-stale `prev.cwd` — closes two convergence bugs the
   * adversarial review found:
   *
   * 1. **Visibility flip (visible → hidden)**: backend now ships
   *    `cwd=""`; we update the entry's routing cwd to "" so the
   *    aggregate stops counting this session. (Reverse flip:
   *    backend ships the real cwd, we restore.)
   *
   * 2. **Delta before `session_created`**: working-mode-flagged
   *    sessions never get a `session_created` (broadcaster filters
   *    them at session_ws_broadcaster.py:141). When they later
   *    transition to visible (`working_mode` cleared), the first
   *    signal is a `monitoring_changed`/`unread_changed` with a real
   *    cwd — we materialize the entry from the payload instead of
   *    silently dropping it.
   *
   * Running-ness for the aggregate is the projection
   * `monitoring_state !== "stopped"`, so an entry crossing that boundary
   * recomputes its project. Phantom-entry protection still holds: a delta
   * arriving with `cwd === ""` for an unknown sid is dropped. */
  private applyRoutedDelta(
    sid: string,
    payloadCwd: string,
    payloadNode: string,
    patch: { monitoring_state?: MonitoringState; unread_count?: number },
  ) {
    const prev = this.sessions.get(sid);
    if (!prev) {
      // Auto-insert only if the payload indicates visibility. Hidden
      // sessions never seen are still not materialized — no aggregate
      // would account for them anyway.
      if (!payloadCwd) return;
      const inserted: SessionEntry = {
        unread_count: patch.unread_count ?? 0,
        monitoring_state: patch.monitoring_state ?? "stopped",
        cwd: payloadCwd,
        node_id: payloadNode,
        markers: {},
      };
      this.sessions.set(sid, inserted);
      this.recomputeProject(payloadCwd, payloadNode);
      this.version += 1;
      this.notifySession(sid);
      this.notifyProject(payloadCwd, payloadNode);
      return;
    }

    const nextState = patch.monitoring_state ?? prev.monitoring_state;
    const nextUnread = patch.unread_count ?? prev.unread_count;
    // If the payload's routing key differs from what we have
    // (visibility flip, or cwd migration we missed), migrate. We
    // keep the entry's `cwd` aligned with the payload because the
    // aggregate sums over `entry.cwd === project.cwd`.
    const routingChanged =
      payloadCwd !== prev.cwd || payloadNode !== prev.node_id;
    const valueChanged =
      nextState !== prev.monitoring_state || nextUnread !== prev.unread_count;
    if (!routingChanged && !valueChanged) return;

    this.sessions.set(sid, {
      unread_count: nextUnread,
      monitoring_state: nextState,
      cwd: payloadCwd,
      node_id: payloadNode,
      markers: prev.markers,
    });
    this.version += 1;
    this.notifySession(sid);
    // The project aggregate only moves when running-ness crosses the
    // stopped boundary, unread changes, or the session migrates projects —
    // NOT on an active↔idle↔waiting flip (still running). Skip the project
    // recompute/notify otherwise so badge re-renders don't storm.
    const projectChanged =
      isRunning(nextState) !== isRunning(prev.monitoring_state) ||
      nextUnread !== prev.unread_count ||
      routingChanged;
    if (projectChanged) {
      this.recomputeProject(prev.cwd, prev.node_id);
      if (routingChanged) this.recomputeProject(payloadCwd, payloadNode);
      this.notifyProject(prev.cwd, prev.node_id);
      if (routingChanged) this.notifyProject(payloadCwd, payloadNode);
    }
  }

  private onCreated(d: SessionCreatedPayload) {
    const sess = d.session;
    if (!sess?.id) return;
    // Idempotent: `session_created` may arrive after the session is
    // already in the snapshot (bootstrap raced with creation, or a
    // buffered created lands after a refresh covers it).
    if (this.sessions.has(sess.id)) return;
    const entry: SessionEntry = {
      unread_count: Math.max(0, Number(sess.unread_count) || 0),
      monitoring_state: sess.is_running ? "active" : "stopped",
      cwd: sess.cwd ?? "",
      node_id: sess.node_id || "primary",
      markers: {},
      testape_active: false,
    };
    this.sessions.set(sess.id, entry);
    this.recomputeAndNotifySession(sess.id, entry.cwd, entry.node_id);
  }

  private onDeleted(d: SessionDeletedPayload) {
    if (!d?.session_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return;
    this.sessions.delete(d.session_id);
    this.metaCache.delete(d.session_id);
    this.recomputeAndNotifySession(d.session_id, prev.cwd, prev.node_id);
  }

  private onMetadataUpdated(d: SessionMetadataPayload) {
    if (!d?.session_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return;
    const patch = d.patch ?? {};
    // Only handle the per-project routing keys here. Other metadata
    // fields (name, model, pinned, ...) don't affect aggregates and
    // are consumed by App-level handlers elsewhere.
    if (patch.cwd === undefined && patch.node_id === undefined) return;
    const newCwd = patch.cwd ?? prev.cwd;
    const newNode = patch.node_id ?? prev.node_id;
    if (newCwd === prev.cwd && newNode === prev.node_id) return;
    this.sessions.set(d.session_id, { ...prev, cwd: newCwd, node_id: newNode });
    // Migrate: recompute BOTH the old and new project's aggregate.
    this.recomputeProject(prev.cwd, prev.node_id);
    this.recomputeProject(newCwd, newNode);
    this.notifySession(d.session_id);
    this.notifyProject(prev.cwd, prev.node_id);
    this.notifyProject(newCwd, newNode);
    this.version += 1;
  }

  // ── Aggregate derivation ─────────────────────────────────────────

  private deriveAllProjects(
    sessions: Map<string, SessionEntry>,
  ): Map<string, ProjectAggregate> {
    const out = new Map<string, ProjectAggregate>();
    for (const entry of sessions.values()) {
      if (!entry.cwd) continue;
      const key = projectKey(entry.cwd, entry.node_id);
      let agg = out.get(key);
      if (!agg) {
        agg = { running_count: 0, unread_session_count: 0 };
        out.set(key, agg);
      }
      if (isRunning(entry.monitoring_state)) agg.running_count += 1;
      if (entry.unread_count > 0) agg.unread_session_count += 1;
    }
    return out;
  }

  /** Recompute one project's aggregate by summing matching sessions.
   * Cheaper than maintaining incremental ±delta math (which drifts on
   * any missed event). At ~200 sessions this is a microsecond
   * iteration — paid only on the affected project, per delta. */
  private recomputeProject(cwd: string, nodeId: string) {
    if (!cwd) return; // hidden — no aggregate to recompute
    const key = projectKey(cwd, nodeId);
    let running = 0;
    let unreadSessions = 0;
    for (const entry of this.sessions.values()) {
      if (entry.cwd !== cwd || entry.node_id !== nodeId) continue;
      if (isRunning(entry.monitoring_state)) running += 1;
      if (entry.unread_count > 0) unreadSessions += 1;
    }
    if (running === 0 && unreadSessions === 0) {
      this.projects.delete(key);
    } else {
      this.projects.set(key, {
        running_count: running,
        unread_session_count: unreadSessions,
      });
    }
  }

  private recomputeAndNotifySession(sid: string, cwd: string, nodeId: string) {
    this.recomputeProject(cwd, nodeId);
    this.version += 1;
    this.notifySession(sid);
    this.notifyProject(cwd, nodeId);
  }

  private notifySession(sid: string) {
    const ls = this.sessionListeners.get(sid);
    if (ls) for (const fn of ls) fn();
  }

  private notifyProject(cwd: string, nodeId: string) {
    if (!cwd) return;
    const ls = this.projectListeners.get(projectKey(cwd, nodeId));
    if (ls) for (const fn of ls) fn();
  }

  private notifyAll() {
    for (const ls of this.sessionListeners.values()) {
      for (const fn of ls) fn();
    }
    for (const ls of this.projectListeners.values()) {
      for (const fn of ls) fn();
    }
  }

  // ── Public readers ───────────────────────────────────────────────

  // Stable-reference cache for the public `SessionMeta` shape.
  // INVARIANT: `getSession(sid)` must return the SAME object reference
  // between two mutations — `useSyncExternalStore` calls `getSnapshot`
  // every render and equality-checks the result; a fresh allocation
  // each call triggers an infinite render loop ("The result of
  // getSnapshot should be cached").
  private metaCache = new Map<string, SessionMeta>();

  getSession(sid: string): SessionMeta {
    const e = this.sessions.get(sid);
    if (!e) return EMPTY_SESSION;
    const cached = this.metaCache.get(sid);
    let result: SessionMeta;
    if (
      cached &&
      cached.unread_count === e.unread_count &&
      cached.monitoring_state === e.monitoring_state &&
      cached.markers === e.markers &&
      cached.testape_active === e.testape_active
    ) {
      result = cached;
    } else {
      result = {
        is_running: isRunning(e.monitoring_state),
        unread_count: e.unread_count,
        monitoring_state: e.monitoring_state,
        markers: e.markers,
        testape_active: !!e.testape_active,
      };
      this.metaCache.set(sid, result);
    }
    // TEMP DEBUG #185: consecutive calls must return the same ref between
    // mutations. Log when a sid's returned ref changes identity.
    const _dbg = (window.__gsLast ??= {});
    const _prev = _dbg[sid];
    if (_prev && _prev !== result) {
      (window.__gsJitterList ??= []).push({
        sid: sid.slice(0, 8),
        pU: _prev.unread_count, cU: result.unread_count,
        pS: _prev.monitoring_state, cS: result.monitoring_state,
        pT: _prev.testape_active, cT: result.testape_active,
        sameMarkers: _prev.markers === result.markers,
        sameUnread: _prev.unread_count === result.unread_count,
        sameState: _prev.monitoring_state === result.monitoring_state,
      });
      if ((window.__gsJitterList as unknown[]).length > 80) (window.__gsJitterList as unknown[]).length = 80;
    }
    _dbg[sid] = result;
    return result;
  }

  getProject(path: string, nodeId: string): ProjectAggregate {
    return (
      this.projects.get(projectKey(path, nodeId || "primary")) ??
      EMPTY_AGGREGATE
    );
  }

  /** Live `SessionMeta` for a sid, or null if the registry has no entry for
   * it (vs `getSession`, which returns the shared EMPTY_SESSION). Status
   * sort uses this to decide live-rank vs page-row fallback. */
  peekMeta(sid: string): SessionMeta | null {
    return this.sessions.has(sid) ? this.getSession(sid) : null;
  }

  /** Seed entries from a loaded `/api/sessions` page so deeper-page rows
   * (beyond the bootstrap's first page) have a registry entry for both
   * status rank AND the running/unread badge. Only FILLS missing sids —
   * never overwrites a live entry, which may be fresher than the page
   * snapshot. */
  seedFromRows(rows: Array<{
    id?: string;
    is_running?: boolean;
    unread_count?: number;
    cwd?: string;
    node_id?: string;
    monitoring_state?: string;
    markers?: Record<string, MarkerInfo>;
  }>): void {
    let changed = false;
    for (const s of rows) {
      if (!s?.id || this.sessions.has(s.id)) continue;
      const ms: MonitoringState = (s.monitoring_state as MonitoringState)
        || (s.is_running ? "active" : "stopped");
      this.sessions.set(s.id, {
        unread_count: Math.max(0, Number(s.unread_count) || 0),
        monitoring_state: ms,
        cwd: s.cwd ?? "",
        node_id: s.node_id || "primary",
        markers: (s.markers && typeof s.markers === "object") ? s.markers : {},
        testape_active: false,
      });
      this.recomputeProject(s.cwd ?? "", s.node_id || "primary");
      this.notifySession(s.id);
      changed = true;
    }
    if (changed) this.version += 1;
  }

  subscribeSession(sid: string, fn: Listener): () => void {
    let set = this.sessionListeners.get(sid);
    if (!set) {
      set = new Set();
      this.sessionListeners.set(sid, set);
    }
    set.add(fn);
    return () => {
      set?.delete(fn);
      if (set?.size === 0) this.sessionListeners.delete(sid);
    };
  }

  /** Test-only escape hatch — wipes the registry to fresh post-`bind`
   * state (sessions/projects/deltabuffer cleared, `_bootstrapped` =
   * false). Production code never calls this; vitest uses it between
   * tests so the module-level singleton doesn't leak state across
   * cases. Listener sets are preserved so subscriptions registered
   * by `useSyncExternalStore` mounts from earlier tests don't dangle. */
  __resetForTests() {
    this.sessions.clear();
    this.projects.clear();
    this.metaCache.clear();
    this._deltaBuffer = [];
    this._bootstrapped = false;
    this._bootstrapInFlight = null;
    this.version += 1;
  }

  subscribeProject(path: string, nodeId: string, fn: Listener): () => void {
    const key = projectKey(path, nodeId || "primary");
    let set = this.projectListeners.get(key);
    if (!set) {
      set = new Set();
      this.projectListeners.set(key, set);
    }
    set.add(fn);
    return () => {
      set?.delete(fn);
      if (set?.size === 0) this.projectListeners.delete(key);
    };
  }
}

function projectKey(path: string, nodeId: string): string {
  return `${nodeId}::${path}`;
}

// Module-level singleton. Bound at App mount via `sessionRegistry.bind()`.
export const sessionRegistry = new SessionRegistry();

// Status-sort tags + states. MUST mirror the backend `_session_status_rank`
// in `backend/main.py` (parity locked by a test) — same buckets, same
// highest-wins precedence.
const MARKER_TAG_NEEDS_DECISION = "NEEDS_USER_DECISION";
const MARKER_TAG_ALL_TASKS_DONE = "ALL_TASKS__DONE";
const RUNNING_STATES = new Set<string>(["active", "waiting_on_background"]);

interface StatusFields {
  monitoring_state?: string;
  unread_count?: number;
  markers?: Record<string, MarkerInfo>;
}

/** Status bucket (4 running → 0 none) for a session's live or row-snapshot
 * status fields. Higher sorts first. Mirrors the backend rank exactly. */
export function statusRankOf(s: StatusFields): number {
  const state = s.monitoring_state ?? "stopped";
  if (RUNNING_STATES.has(state)) return 4;
  const tags = new Set(
    Object.values(s.markers ?? {}).map((m) => m?.tag).filter(Boolean),
  );
  if (state === "blocked_on_user" || tags.has(MARKER_TAG_NEEDS_DECISION)) return 3;
  if ((s.unread_count ?? 0) > 0) return 2;
  if (tags.has(MARKER_TAG_ALL_TASKS_DONE)) return 1;
  return 0;
}

/** Rank for a session row: prefer the LIVE registry entry (so it agrees with
 * the rendered badge); fall back to the row's own decorate fields when the
 * registry has no entry yet (deeper page not yet seeded). */
export function statusRankForRow(session: {
  id: string;
  monitoring_state?: string;
  unread_count?: number;
  markers?: Record<string, MarkerInfo>;
}): number {
  const live = sessionRegistry.peekMeta(session.id);
  if (live) return statusRankOf(live);
  return statusRankOf({
    monitoring_state: session.monitoring_state,
    unread_count: session.unread_count,
    markers: session.markers,
  });
}

export function useSessionMeta(sid: string | null | undefined): SessionMeta {
  return useSyncExternalStore(
    (cb) => {
      if (!sid) return () => {};
      return sessionRegistry.subscribeSession(sid, cb);
    },
    () => (sid ? sessionRegistry.getSession(sid) : EMPTY_SESSION),
    () => (sid ? sessionRegistry.getSession(sid) : EMPTY_SESSION),
  );
}

export function useProjectAggregate(
  path: string | null | undefined,
  nodeId: string = "primary",
): ProjectAggregate {
  return useSyncExternalStore(
    (cb) => {
      if (!path) return () => {};
      return sessionRegistry.subscribeProject(path, nodeId, cb);
    },
    () => (path ? sessionRegistry.getProject(path, nodeId) : EMPTY_AGGREGATE),
    () => (path ? sessionRegistry.getProject(path, nodeId) : EMPTY_AGGREGATE),
  );
}

/** Imperative ack — POSTs to `/api/sessions/:sid/seen`. The backend
 * fires `session_unread_changed{unread_count:0}` which the registry
 * picks up via the bus, so consumers update without waiting on the
 * POST's response. */
export async function ackSessionSeen(sid: string, uid?: string | null) {
  try {
    await fetch(`${API}/api/sessions/${encodeURIComponent(sid)}/seen`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid ?? null }),
    });
  } catch {
    // Failure is silent — the unread counter stays "stuck" until the
    // next event arrives or the user re-focuses. No retry loop on
    // purpose; the registry will reconcile on the next bootstrap or
    // WS reconnect.
  }
}
