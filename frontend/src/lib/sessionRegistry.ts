import { useSyncExternalStore } from "react";

import { API } from "../api";
import {
  SESSION_STATUS_KEYS,
  type ProjectAggregateRecord,
  type ProjectAggregatesChangedData,
  type ProjectsSnapshot,
  type SessionStatusKey,
  type TaskItem,
  type TodoItem,
} from "../types";
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
  // Boolean setting of the owning extension that gates `sound`. Absent means
  // the sound is ungated.
  sound_setting?: string;
  // Source tag (e.g. NEEDS_USER_DECISION / ALL_TASKS__DONE). Set by the
  // backend at marker-detect time so status sort classifies by tag, never
  // by drifting color/tooltip.
  tag?: string;
}

export interface SessionMeta {
  is_running: boolean;
  unread_count: number;
  pending_user_input_count: number;
  monitoring_state: MonitoringState;
  markers: Record<string, MarkerInfo>;
  has_error: boolean;
  current_todos: TodoItem[];
  current_tasks: TaskItem[];
  status_key: SessionStatusKey;
  status_rank: number;
}

export interface ProjectAggregate {
  running_count: number;
  unread_session_count: number;
  waiting_for_user_count: number;
  errored_count: number;
}

type SessionRegistryRow = {
  id?: string;
  is_running?: boolean;
  unread_count?: number;
  pending_user_input_count?: number;
  cwd?: string;
  node_id?: string;
  monitoring_state?: string;
  markers?: Record<string, MarkerInfo>;
  has_error?: boolean;
  unseen_error?: unknown;
  current_todos?: TodoItem[];
  current_tasks?: TaskItem[];
  status_key?: SessionStatusKey;
  status_rank?: number;
};

// Internal per-session record. `monitoring_state` is the SINGLE source of
// session state — `is_running` is the derived projection
// `monitoring_state !== "stopped"`, computed in `getSession`/aggregates, never
// stored. Carries `(cwd, node_id)` to route deltas to the right project
// aggregate; NOT exposed to consumers — `useSessionMeta` projects to
// `SessionMeta`.
interface SessionEntry {
  unread_count: number;
  pending_user_input_count: number;
  monitoring_state: MonitoringState;
  cwd: string;
  node_id: string;
  markers: Record<string, MarkerInfo>;
  has_error: boolean;
  current_todos: TodoItem[];
  current_tasks: TaskItem[];
  status_key: SessionStatusKey;
  status_rank: number;
}

/** The one place `is_running` is defined, mirroring the backend's
 * `session_status.SessionStatus.busy`: a session is running iff it has
 * foreground work in flight ("active") or background work still
 * finishing ("waiting_on_background").
 *
 * "idle" and "blocked_on_user" are live processes with no work in
 * flight — they are Idle on the Running dimension and carry their signal
 * on the waiting-on-user dimension instead, so one dimension never
 * masks the other. */
function isRunning(state: MonitoringState): boolean {
  return state === "active" || state === "waiting_on_background";
}

/** Waiting-on-user, mirroring the backend dimension: a pending
 * user-facing request, an approval blocking the turn, or a
 * NEEDS_USER_DECISION marker on the latest turn. */
function entryFromRow(row: SessionRegistryRow): SessionEntry {
  const monitoringState: MonitoringState = (row.monitoring_state as MonitoringState)
    || (row.is_running ? "active" : "stopped");
  return {
    unread_count: Math.max(0, Number(row.unread_count) || 0),
    pending_user_input_count: Math.max(0, Number(row.pending_user_input_count) || 0),
    monitoring_state: monitoringState,
    cwd: row.cwd ?? "",
    node_id: row.node_id || "primary",
    markers: (row.markers && typeof row.markers === "object") ? row.markers : {},
    has_error: !!row.has_error || !!row.unseen_error,
    current_todos: Array.isArray(row.current_todos) ? row.current_todos : [],
    current_tasks: Array.isArray(row.current_tasks) ? row.current_tasks : [],
    status_key: row.status_key ?? "idle",
    status_rank: Math.max(0, Number(row.status_rank) || 0),
  };
}

const EMPTY_MARKERS: Record<string, MarkerInfo> = {};
const EMPTY_SESSION: SessionMeta = {
  is_running: false,
  unread_count: 0,
  pending_user_input_count: 0,
  monitoring_state: "stopped",
  markers: EMPTY_MARKERS,
  has_error: false,
  current_todos: [],
  current_tasks: [],
  status_key: "idle",
  status_rank: 0,
};
const EMPTY_AGGREGATE: ProjectAggregate = {
  running_count: 0,
  unread_session_count: 0,
  waiting_for_user_count: 0,
  errored_count: 0,
};

type Listener = () => void;
type BufferedDelta =
  | { type: "session_monitoring_changed"; payload: SessionMonitoringPayload }
  | { type: "session_unread_changed"; payload: SessionUnreadPayload }
  | { type: "session_error_changed"; payload: SessionErrorPayload }
  | { type: "session_user_input_changed"; payload: SessionUserInputPayload }
  | { type: "session_marker_changed"; payload: SessionMarkerPayload }
  | { type: "session_created"; payload: SessionCreatedPayload }
  | { type: "session_deleted"; payload: SessionDeletedPayload }
  | { type: "session_metadata_updated"; payload: SessionMetadataPayload };

interface SessionStatusPayload {
  session_id: string;
  status_key: SessionStatusKey;
  status_rank: number;
}

export interface ProjectSnapshotToken {
  sequence: number;
  epochAtDispatch: string | null;
}

// Carries (cwd, node_id) so it can route the project aggregate +
// materialize a not-yet-seen session.
interface SessionMonitoringPayload {
  session_id: string;
  monitoring_state: MonitoringState;
  cwd?: string;
  node_id?: string;
}
interface SessionUnreadPayload {
  session_id: string;
  unread_count: number;
  cwd?: string;
  node_id?: string;
}
interface SessionErrorPayload {
  session_id: string;
  has_error: boolean;
  cwd?: string;
  node_id?: string;
}
interface SessionUserInputPayload {
  session_id?: string;
  app_session_id?: string;
  pending_user_input_count: number;
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
    monitoring_state?: string;
    unread_count?: number;
    pending_user_input_count?: number;
    current_todos?: TodoItem[];
    current_tasks?: TaskItem[];
  };
}
interface SessionDeletedPayload {
  session_id?: string;
}
interface SessionMetadataPayload {
  session_id: string;
  patch?: {
    // Set (non-empty) means the session became sidebar-hidden, which is
    // the same signal an empty `cwd` carries on a routed delta.
    working_mode?: string | null;
    cwd?: string;
    node_id?: string;
    current_todos?: TodoItem[];
    current_tasks?: TaskItem[];
  };
}

class SessionRegistry {
  // Per-sid entry — populated by `/api/sessions` pages + WS deltas.
  // Sessions enter the map via a page merge (`mergeFromRows` /
  // `seedFromRows`), `session_created`, or a routed delta whose payload
  // carries a visible cwd (`applyRoutedDelta`). A delta arriving with an
  // empty cwd for an unknown sid is dropped — that is the phantom-entry
  // guard against sessions the backend filters server-side.
  // Entries leave the map ONLY via `session_deleted`: `/api/sessions` is
  // paginated and filtered, so absence from a page is not evidence of
  // removal.
  private sessions: Map<string, SessionEntry> = new Map();

  // Backend-authored project aggregate projection keyed by `<node_id>::<cwd>`.
  private projects: Map<string, ProjectAggregate> = new Map();
  private _projectEpoch: string | null = null;
  private _projectRevision = -1;
  private _projectHydrated = false;
  private _projectSnapshotInFlight: Promise<void> | null = null;
  private _projectSnapshotRefreshQueued = false;
  private _projectSnapshotRequestSeq = 0;
  private _lastAppliedProjectSnapshotSeq = 0;
  private _pendingProjectDeltas = new Map<number, ProjectAggregatesChangedData>();

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

  // Tracks whether the WS has connected at least once since `bind()`.
  // The FIRST `connected: true` is the initial handshake — the
  // mount-time `bootstrap()` call already covers it, so re-bootstrapping
  // there would duplicate the `/api/sessions` fetch. Only a SUBSEQUENT
  // `connected: true` (an actual reconnect) needs the resync, since
  // that's when a `running_changed`/`monitoring_changed` ping could have
  // been dropped during the gap. See the `ws_connection_changed`
  // subscription in `bind()`.
  private _wsConnectedOnce = false;

  // Monotonic clock over per-session mutations, plus each sid's last
  // tick. A `/api/sessions` page is a snapshot taken when the request was
  // dispatched; any sid mutated after that watermark has a newer truth
  // locally, so the page's row for it must be discarded rather than
  // applied on top. See `beginPageFetch` / `mergeFromRows`.
  private _deltaSeq = 0;
  private _lastDeltaSeqBySid = new Map<string, number>();

  /** Detach the bus subscriptions + DOM listeners wired by `bind()`.
   * Without this, a `bind()` caller that itself unmounts (e.g. React's
   * `<App>` in a test that mounts/unmounts many times per process)
   * leaves `onResume` permanently bound to `document`/`window` — a
   * later, unrelated `focus`/`visibilitychange` event then fires a
   * stray `bootstrap()` REST call against whatever `fetch` happens to
   * be mocked at that moment. */
  unbind() {
    if (this.busUnsub) {
      this.busUnsub();
      this.busUnsub = null;
    }
    if (this.domUnsub) {
      this.domUnsub();
      this.domUnsub = null;
    }
  }

  /** Wire bus subscriptions + DOM lifecycle. Idempotent — calling
   * twice detaches the prior wire-up first. */
  bind() {
    this.unbind();
    this._wsConnectedOnce = false;

    this.busUnsub = subscribeMany([
      ["session_monitoring_changed", (p) => {
        this.dispatch("session_monitoring_changed", p as SessionMonitoringPayload);
      }],
      ["session_unread_changed", (p) => {
        this.dispatch("session_unread_changed", p as SessionUnreadPayload);
      }],
      ["session_error_changed", (p) => {
        this.dispatch("session_error_changed", p as SessionErrorPayload);
      }],
      ["session_user_input_changed", (p) => {
        this.dispatch("session_user_input_changed", p as SessionUserInputPayload);
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
      ["session_status_changed", (p) => {
        this.onStatusChanged(p as SessionStatusPayload);
      }],
      ["project_aggregates_changed", (p) => {
        this.onProjectAggregatesChanged(p as ProjectAggregatesChangedData);
      }],
      // Drift recovery for a reconnect gap. `session_monitoring_changed`
      // rides `broadcast_global` — a fire-and-forget ping to every connected
      // socket with no events.jsonl persistence and no replay.
      // A ping dropped while this tab's socket was briefly disconnected
      // (network blip, backend restart) is gone forever: nothing else
      // re-derives it, and a tab that stays continuously focused never
      // fires `visibilitychange` to trigger the resume-bootstrap below.
      // Without this, a session can render stuck on stale running/
      // monitoring state indefinitely, until an unrelated action (new
      // prompt, tab switch) happens to trigger a resync as a side
      // effect. The FIRST `connected: true` is the initial handshake,
      // already covered by the mount-time bootstrap — only a later one
      // (an actual reconnect) triggers this.
      ["ws_connection_changed", (p) => {
        if (!(p as { connected?: boolean }).connected) return;
        if (!this._wsConnectedOnce) {
          this._wsConnectedOnce = true;
          return;
        }
        void this.bootstrap();
        void this.refreshProjectSnapshot();
      }],
    ]);

    // Drift recovery: when the tab comes back into focus, re-snapshot.
    // With the single-REST bootstrap this is one `/api/sessions` call.
    // Same handler covers both `visibilitychange` (becoming visible)
    // and explicit `focus` (some browsers fire one but not the other).
    const onResume = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      void this.bootstrap();
      void this.refreshProjectSnapshot();
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
    const dispatchedAtSeq = this.beginPageFetch();
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
      : []) as SessionRegistryRow[];
    if (
      json
      && typeof json === "object"
      && (json as { snapshot_complete?: unknown }).snapshot_complete === false
    ) {
      window.setTimeout(() => {
        void this.bootstrap();
      }, 150);
      return;
    }

    this.mergeFromRows(rows, dispatchedAtSeq);
  }

  /** Watermark for a `/api/sessions` page fetch. Read it BEFORE issuing
   * the request and hand it back to `mergeFromRows`, so entries a WS
   * delta moved while the request was in flight survive the merge. */
  beginPageFetch(): number {
    return this._deltaSeq;
  }

  /** Apply a `/api/sessions` page to the registry.
   *
   * `/api/sessions` is paginated (`offset`/`limit`, default 50) and
   * filtered (archived excluded; folder/pinned/empty sessions outrank
   * recency), so a page is NEVER a complete snapshot of the session
   * universe. Sids absent from it are kept — the only eviction path is
   * an explicit `session_deleted`. Wiping them instead degraded
   * `getSession(sid)` to EMPTY_SESSION (`monitoring_state: "stopped"`)
   * for every session below the first page, which silently reported the
   * OPEN session as not running and collapsed its live turn group in the
   * chat panel with no user action.
   *
   * `dispatchedAtSeq` is the `beginPageFetch()` watermark taken when the
   * request was issued. Any sid mutated after it — by a WS delta OR by
   * another page that already landed — holds the newer truth, so its row
   * is discarded rather than allowed to regress the entry. Applied rows
   * stamp the clock themselves, which is what makes two concurrently
   * in-flight pages (registry bootstrap + sidebar refresh) resolve to the
   * later-landing one instead of the later-resolving one. */
  mergeFromRows(rows: SessionRegistryRow[], dispatchedAtSeq: number): void {
    const nextSessions = new Map<string, SessionEntry>(this.sessions);
    for (const s of rows) {
      if (!s?.id) continue;
      if ((this._lastDeltaSeqBySid.get(s.id) ?? 0) > dispatchedAtSeq) continue;
      nextSessions.set(s.id, entryFromRow(s));
      this._lastDeltaSeqBySid.set(s.id, ++this._deltaSeq);
    }
    this.sessions = nextSessions;

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
      case "session_monitoring_changed":
        return this.onMonitoring(ev.payload);
      case "session_unread_changed":
        return this.onUnread(ev.payload);
      case "session_error_changed":
        return this.onError(ev.payload);
      case "session_user_input_changed":
        return this.onUserInput(ev.payload);
      case "session_marker_changed":
        return this.onMarker(ev.payload);
      case "session_created":
        return this.onCreated(ev.payload);
      case "session_deleted":
        return this.onDeleted(ev.payload);
      case "session_metadata_updated":
        return this.onMetadataUpdated(ev.payload);
    }
  }

  // ── Per-event handlers ───────────────────────────────────────────

  private onMonitoring(d: SessionMonitoringPayload) {
    if (!d.session_id) return;
    this.applyRoutedDelta(d.session_id, d.cwd ?? "", d.node_id ?? "primary", {
      monitoring_state: d.monitoring_state,
    });
  }

  private onUnread(d: SessionUnreadPayload) {
    if (!d.session_id) return;
    this.applyRoutedDelta(d.session_id, d.cwd ?? "", d.node_id ?? "primary", {
      unread_count: Math.max(0, Number(d.unread_count) || 0),
    });
  }

  private commitEntry(sid: string, next: SessionEntry) {
    this.sessions.set(sid, next);
    this.notifySession(sid);
  }

  private onError(d: SessionErrorPayload) {
    if (!d.session_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return; // error dot doesn't materialize a session
    const next = !!d.has_error;
    if (prev.has_error === next) return;
    this.commitEntry(d.session_id, { ...prev, has_error: next });
  }

  private onUserInput(d: SessionUserInputPayload) {
    const sid = d.session_id || d.app_session_id || "";
    if (!sid) return;
    const prev = this.sessions.get(sid);
    if (!prev) return; // input dot doesn't materialize a session
    const next = Math.max(0, Number(d.pending_user_input_count) || 0);
    if (prev.pending_user_input_count === next) return;
    this.commitEntry(sid, { ...prev, pending_user_input_count: next });
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
    this.commitEntry(d.session_id, { ...prev, markers });
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
        pending_user_input_count: 0,
        monitoring_state: patch.monitoring_state ?? "stopped",
        cwd: payloadCwd,
        node_id: payloadNode,
        markers: {},
        has_error: false,
        current_todos: [],
        current_tasks: [],
        status_key: "idle",
        status_rank: 0,
      };
      this.sessions.set(sid, inserted);
      this.notifySession(sid);
      return;
    }

    const nextState = patch.monitoring_state ?? prev.monitoring_state;
    const nextUnread = patch.unread_count ?? prev.unread_count;
    const routingChanged =
      payloadCwd !== prev.cwd || payloadNode !== prev.node_id;
    const valueChanged =
      nextState !== prev.monitoring_state || nextUnread !== prev.unread_count;
    if (!routingChanged && !valueChanged) return;

    const next: SessionEntry = {
      unread_count: nextUnread,
      pending_user_input_count: prev.pending_user_input_count,
      monitoring_state: nextState,
      cwd: payloadCwd,
      node_id: payloadNode,
      markers: prev.markers,
      has_error: prev.has_error,
      current_todos: prev.current_todos,
      current_tasks: prev.current_tasks,
      status_key: prev.status_key,
      status_rank: prev.status_rank,
    };
    this.commitEntry(sid, next);
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
      pending_user_input_count: Math.max(0, Number(sess.pending_user_input_count) || 0),
      monitoring_state: (sess.monitoring_state as MonitoringState)
        || (sess.is_running ? "active" : "stopped"),
      cwd: sess.cwd ?? "",
      node_id: sess.node_id || "primary",
      markers: {},
      has_error: false,
      current_todos: Array.isArray(sess.current_todos) ? sess.current_todos : [],
      current_tasks: Array.isArray(sess.current_tasks) ? sess.current_tasks : [],
      status_key: "idle",
      status_rank: 0,
    };
    this.sessions.set(sess.id, entry);
    this.notifySession(sess.id);
  }

  private onDeleted(d: SessionDeletedPayload) {
    if (!d?.session_id) return;
    if (!this.sessions.delete(d.session_id)) return;
    this.metaCache.delete(d.session_id);
    this.notifySession(d.session_id);
  }

  private onMetadataUpdated(d: SessionMetadataPayload) {
    if (!d?.session_id) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return;
    const patch = d.patch ?? {};
    const hasTodoPatch =
      patch.current_todos !== undefined || patch.current_tasks !== undefined;
    if (
      patch.cwd === undefined &&
      patch.node_id === undefined &&
      patch.working_mode === undefined &&
      !hasTodoPatch
    ) return;
    const hiddenNow = patch.working_mode !== undefined && !!patch.working_mode;
    const newCwd = hiddenNow ? "" : (patch.cwd ?? prev.cwd);
    const newNode = patch.node_id ?? prev.node_id;
    const nextTodos = patch.current_todos ?? prev.current_todos;
    const nextTasks = patch.current_tasks ?? prev.current_tasks;
    if (
      newCwd === prev.cwd &&
      newNode === prev.node_id &&
      nextTodos === prev.current_todos &&
      nextTasks === prev.current_tasks
    ) return;
    this.sessions.set(d.session_id, {
      ...prev,
      cwd: newCwd,
      node_id: newNode,
      current_todos: nextTodos,
      current_tasks: nextTasks,
    });
    this.notifySession(d.session_id);
  }

  private onStatusChanged(d: SessionStatusPayload) {
    if (
      !d?.session_id
      || !SESSION_STATUS_KEYS.includes(d.status_key)
      || !Number.isSafeInteger(d.status_rank)
      || d.status_rank < 0
    ) return;
    const prev = this.sessions.get(d.session_id);
    if (!prev) return;
    if (prev.status_key === d.status_key && prev.status_rank === d.status_rank) return;
    this.commitEntry(d.session_id, {
      ...prev,
      status_key: d.status_key,
      status_rank: d.status_rank,
    });
  }

  beginProjectSnapshotFetch(): ProjectSnapshotToken {
    return {
      sequence: ++this._projectSnapshotRequestSeq,
      epochAtDispatch: this._projectEpoch,
    };
  }

  applyProjectSnapshot(
    value: unknown,
    token: ProjectSnapshotToken = this.beginProjectSnapshotFetch(),
  ): boolean {
    const snapshot = parseProjectSnapshot(value);
    if (!snapshot) return false;
    if (token.sequence < this._lastAppliedProjectSnapshotSeq) return false;
    if (
      token.epochAtDispatch !== this._projectEpoch
      && snapshot.epoch !== this._projectEpoch
    ) return false;
    if (
      snapshot.epoch === this._projectEpoch
      && this._projectHydrated
      && snapshot.revision < this._projectRevision
    ) return false;

    const next = new Map<string, ProjectAggregate>();
    for (const project of snapshot.projects) {
      next.set(projectKey(project.path, project.node_id), aggregateFromRecord(project));
    }
    this.replaceProjects(next);
    this._projectEpoch = snapshot.epoch;
    this._projectRevision = snapshot.revision;
    this._projectHydrated = true;
    this._lastAppliedProjectSnapshotSeq = token.sequence;
    this._pendingProjectDeltas = new Map(
      [...this._pendingProjectDeltas].filter(([, delta]) => delta.epoch === snapshot.epoch),
    );
    this.drainProjectDeltas();
    return true;
  }

  refreshProjectSnapshot(): Promise<void> {
    if (this._projectSnapshotInFlight) {
      this._projectSnapshotRefreshQueued = true;
      return this._projectSnapshotInFlight;
    }
    const token = this.beginProjectSnapshotFetch();
    this._projectSnapshotInFlight = (async () => {
      try {
        const response = await fetch(`${API}/api/projects`);
        if (!response.ok) return;
        this.applyProjectSnapshot(await response.json(), token);
      } catch {
        return;
      }
    })().finally(() => {
      this._projectSnapshotInFlight = null;
      if (!this._projectSnapshotRefreshQueued) return;
      this._projectSnapshotRefreshQueued = false;
      void this.refreshProjectSnapshot();
    });
    return this._projectSnapshotInFlight;
  }

  private onProjectAggregatesChanged(delta: ProjectAggregatesChangedData) {
    if (!isProjectAggregateDelta(delta)) return;
    if (delta.epoch !== this._projectEpoch) {
      this._projectEpoch = delta.epoch;
      this._projectRevision = -1;
      this._projectHydrated = false;
      this._pendingProjectDeltas.clear();
      this._pendingProjectDeltas.set(delta.revision, delta);
      this.replaceProjects(new Map());
      void this.refreshProjectSnapshot();
      return;
    }
    if (!this._projectHydrated) {
      this._pendingProjectDeltas.set(delta.revision, delta);
      void this.refreshProjectSnapshot();
      return;
    }
    if (delta.revision <= this._projectRevision) return;
    if (delta.revision > this._projectRevision + 1) {
      this._pendingProjectDeltas.set(delta.revision, delta);
      void this.refreshProjectSnapshot();
      return;
    }
    this.applyProjectDelta(delta);
    this.drainProjectDeltas();
  }

  private drainProjectDeltas() {
    if (!this._projectHydrated) return;
    for (;;) {
      const next = this._pendingProjectDeltas.get(this._projectRevision + 1);
      if (!next) break;
      this._pendingProjectDeltas.delete(next.revision);
      this.applyProjectDelta(next);
    }
    for (const revision of this._pendingProjectDeltas.keys()) {
      if (revision <= this._projectRevision) this._pendingProjectDeltas.delete(revision);
    }
    if (this._pendingProjectDeltas.size > 0) void this.refreshProjectSnapshot();
  }

  private applyProjectDelta(delta: ProjectAggregatesChangedData) {
    const changed = new Set<string>();
    for (const record of delta.upserts) {
      const key = projectKey(record.path, record.node_id);
      const aggregate = aggregateFromRecord(record);
      if (!sameAggregate(this.projects.get(key), aggregate)) {
        this.projects.set(key, aggregate);
        changed.add(key);
      }
    }
    for (const record of delta.tombstones) {
      const key = projectKey(record.path, record.node_id);
      if (this.projects.delete(key)) changed.add(key);
    }
    this._projectRevision = delta.revision;
    this.notifyProjectKeys(changed);
  }

  private replaceProjects(next: Map<string, ProjectAggregate>) {
    const changed = new Set<string>();
    for (const [key, previous] of this.projects) {
      if (!sameAggregate(previous, next.get(key))) changed.add(key);
    }
    for (const [key, aggregate] of next) {
      if (!sameAggregate(this.projects.get(key), aggregate)) changed.add(key);
    }
    this.projects = next;
    this.notifyProjectKeys(changed);
  }

  private notifyProjectKeys(keys: Set<string>) {
    for (const key of keys) {
      const listeners = this.projectListeners.get(key);
      if (listeners) for (const listener of listeners) listener();
    }
  }

  private notifySession(sid: string) {
    // Single funnel for per-session mutations (create, delete, routed
    // delta, metadata, markers, error, seed) — the one place that stamps
    // the mutation clock a page merge compares its watermark against.
    this._lastDeltaSeqBySid.set(sid, ++this._deltaSeq);
    const ls = this.sessionListeners.get(sid);
    if (ls) for (const fn of ls) fn();
  }

  private notifyAll() {
    for (const ls of this.sessionListeners.values()) {
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
    if (
      cached &&
      cached.unread_count === e.unread_count &&
      cached.pending_user_input_count === e.pending_user_input_count &&
      cached.monitoring_state === e.monitoring_state &&
      cached.markers === e.markers &&
      cached.has_error === e.has_error &&
      cached.current_todos === e.current_todos &&
      cached.current_tasks === e.current_tasks &&
      cached.status_key === e.status_key &&
      cached.status_rank === e.status_rank
    ) {
      return cached;
    }
    const next: SessionMeta = {
      is_running: isRunning(e.monitoring_state),
      unread_count: e.unread_count,
      pending_user_input_count: e.pending_user_input_count,
      monitoring_state: e.monitoring_state,
      markers: e.markers,
      has_error: e.has_error,
      current_todos: e.current_todos,
      current_tasks: e.current_tasks,
      status_key: e.status_key,
      status_rank: e.status_rank,
    };
    this.metaCache.set(sid, next);
    return next;
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
  seedFromRows(rows: SessionRegistryRow[]): void {
    for (const s of rows) {
      if (!s?.id || this.sessions.has(s.id)) continue;
      this.sessions.set(s.id, entryFromRow(s));
      this.notifySession(s.id);
    }
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
    this._lastDeltaSeqBySid.clear();
    this._deltaBuffer = [];
    this._bootstrapped = false;
    this._bootstrapInFlight = null;
    this._projectEpoch = null;
    this._projectRevision = -1;
    this._projectHydrated = false;
    this._projectSnapshotInFlight = null;
    this._projectSnapshotRefreshQueued = false;
    this._projectSnapshotRequestSeq = 0;
    this._lastAppliedProjectSnapshotSeq = 0;
    this._pendingProjectDeltas.clear();
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

function aggregateFromRecord(record: ProjectAggregateRecord): ProjectAggregate {
  return {
    running_count: record.running_count,
    unread_session_count: record.unread_session_count,
    waiting_for_user_count: record.waiting_for_user_count,
    errored_count: record.errored_count,
  };
}

function sameAggregate(
  left: ProjectAggregate | undefined,
  right: ProjectAggregate | undefined,
): boolean {
  return left === right || (
    !!left
    && !!right
    && left.running_count === right.running_count
    && left.unread_session_count === right.unread_session_count
    && left.waiting_for_user_count === right.waiting_for_user_count
    && left.errored_count === right.errored_count
  );
}

function isProjectAggregateRecord(value: unknown): value is ProjectAggregateRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return typeof record.path === "string"
    && record.path.length > 0
    && typeof record.node_id === "string"
    && record.node_id.length > 0
    && [
      record.running_count,
      record.unread_session_count,
      record.waiting_for_user_count,
      record.errored_count,
    ].every((count) => Number.isSafeInteger(count) && Number(count) >= 0);
}

function isProjectAggregateDelta(value: unknown): value is ProjectAggregatesChangedData {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const delta = value as Record<string, unknown>;
  return typeof delta.epoch === "string"
    && delta.epoch.length > 0
    && Number.isSafeInteger(delta.revision)
    && Number(delta.revision) >= 0
    && Array.isArray(delta.upserts)
    && delta.upserts.every(isProjectAggregateRecord)
    && Array.isArray(delta.tombstones)
    && delta.tombstones.every(isProjectAggregateRecord);
}

function parseProjectSnapshot(value: unknown): ProjectsSnapshot | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const snapshot = value as Record<string, unknown>;
  if (
    typeof snapshot.epoch !== "string"
    || snapshot.epoch.length === 0
    || !Number.isSafeInteger(snapshot.revision)
    || Number(snapshot.revision) < 0
    || !Array.isArray(snapshot.projects)
  ) return null;
  const projects: Array<ProjectsSnapshot["projects"][number]> = [];
  for (const value of snapshot.projects) {
    if (!isProjectAggregateRecord(value)) return null;
    projects.push(value as ProjectsSnapshot["projects"][number]);
  }
  return {
    projects,
    epoch: snapshot.epoch,
    revision: Number(snapshot.revision),
  };
}

// Module-level singleton. Bound at App mount via `sessionRegistry.bind()`.
export const sessionRegistry = new SessionRegistry();

export { SESSION_STATUS_KEYS };
export type { SessionStatusKey };

/** Rank for a session row: prefer the LIVE registry entry (so it agrees with
 * the rendered badge); fall back to the row's own decorate fields when the
 * registry has no entry yet (deeper page not yet seeded). */
export function statusRankForRow(session: {
  id: string;
  status_rank?: number;
}): number {
  const live = sessionRegistry.peekMeta(session.id);
  return live?.status_rank ?? session.status_rank ?? 0;
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

/** Imperative "mark as unread" — POSTs to `/api/sessions/:sid/unread`.
 * The backend clears the seen watermark and fires
 * `session_unread_changed{unread_count>0}` which the registry picks up
 * via the bus, so the badge appears without waiting on the response. */
export async function markSessionUnread(sid: string) {
  try {
    await fetch(`${API}/api/sessions/${encodeURIComponent(sid)}/unread`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    // Silent — the registry reconciles on the next bootstrap / WS reconnect.
  }
}
