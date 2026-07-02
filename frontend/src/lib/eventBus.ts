/** Typed in-process pub/sub used by the session/project status badges
 * and any future cross-component consumer that subscribes to backend
 * state deltas.
 *
 * INVARIANT: this bus is a pure observer. It does NOT cache state — the
 * `sessionRegistry` (and future singletons) own caching. Subscribers
 * are called synchronously inside `publish`. A subscriber that throws
 * is swallowed so one bad handler can't take down the rest.
 *
 * The shape mirrors the backend's `WSEventType` union as closely as
 * useful — every WS frame the useWebSocket hook receives is pumped
 * into the bus via `publish(type, payload)`. New consumers subscribe
 * by type instead of plumbing a new `onXxx` callback prop through
 * App.tsx.
 */

import type { Schedule, Session } from "../types";

// Map of event type → payload shape. Only events that have a typed
// consumer need to be enumerated here; everything else falls back to
// `unknown` via the generic publish/subscribe overload.
export interface BusEventMap {
  // New unified running / unread frames (replace the old
  // `active_process_counts_changed` window event).
  session_running_changed: {
    session_id: string;
    value: boolean;
  };
  session_unread_changed: {
    session_id: string;
    unread_count: number;
    last_seen_event_uid?: string | null;
  };
  // A turn ended in an unrecoverable error (has_error true) or the dot was
  // retired by a view-ack / successful turn (has_error false). Consumed by
  // the registry + status badge to render the red error dot.
  session_error_changed: {
    session_id: string;
    has_error: boolean;
  };
  // A request_user_input prompt is pending/resolved for a session. The backend
  // sends the authoritative pending count, so badges converge even if multiple
  // input requests overlap.
  session_user_input_changed: {
    session_id: string;
    pending_user_input_count: number;
  };
  // An extension attention marker on a session changed. `marker` null
  // clears that extension's marker.
  session_marker_changed: {
    session_id: string;
    extension_id: string;
    marker: { color: string; tooltip: string; sound?: boolean } | null;
  };
  // Finer per-session monitoring state (active / idle / blocked_on_user /
  // waiting_on_background / stopped). Fires on transitions that don't flip
  // the running boolean. Consumed by the registry + status badge.
  session_monitoring_changed: {
    session_id: string;
    monitoring_state: string;
  };
  // A provenance row was appended; the open Details panel refetches.
  session_provenance_changed: {
    session_id: string;
  };
  // Pre-existing frames the registry cares about — listed so the
  // typed dispatch helpers in `useWebSocket` can `eventBus.publish(...)`
  // them with payload safety.
  projects_changed: Record<string, unknown>;
  session_created: { session: Session };
  session_deleted: { session_id: string };
  session_renamed: { session_id: string; name: string };
  // A runner started/stopped babysitter-lingering — background shells /
  // monitors outlive the turn. Consumed by the session-view strip.
  run_lingering: {
    app_session_id: string;
    run_id: string;
    lingering: boolean;
  };
  run_state: {
    app_session_id: string;
    runs: unknown[];
  };
  turn_start: {
    app_session_id?: string;
    session_id?: string;
  };
  // The session's schedule list changed; payload IS the snapshot.
  schedules_updated: {
    app_session_id: string;
    schedules: Schedule[];
  };
  provider_config_sync_changed: {
    scope: string;
    category: string;
    capability_id: string;
    path: string;
    cwd: string;
  };
  // Global ping — any schedule mutated; refetch GET /api/schedules.
  schedules_changed: Record<string, unknown>;
  extensions_changed: Record<string, unknown>;
  testape_session_state: {
    session_id: string;
    active: boolean;
  };
  // A session row drag started/ended in the sidebar. Pure transient UI
  // fact — published so extensions (e.g. the agent board) can reveal a
  // drop surface while a session is being dragged. Carries the session id
  // on start; end carries nothing.
  session_drag_start: { session_id: string; name?: string };
  session_drag_end: Record<string, never>;
  // A "Copy id" event link was activated — request the target session's Chat
  // to scroll the referenced message into view. Durable target is held in
  // `messageFocus`; this frame only nudges an already-open Chat.
  focus_message: { session_id: string; message_id: string };
}

type Handler<T> = (payload: T) => void;

class EventBus {
  // Per-type subscriber set. Using Set so unsubscribe is O(1) AND
  // duplicate subscriptions (same handler ref) collapse to one.
  private subs: Map<string, Set<Handler<unknown>>> = new Map();

  subscribe<K extends keyof BusEventMap>(
    type: K,
    handler: Handler<BusEventMap[K]>,
  ): () => void;
  subscribe(type: string, handler: Handler<unknown>): () => void;
  subscribe(type: string, handler: Handler<unknown>): () => void {
    let set = this.subs.get(type);
    if (!set) {
      set = new Set();
      this.subs.set(type, set);
    }
    set.add(handler);
    return () => {
      set?.delete(handler);
    };
  }

  publish<K extends keyof BusEventMap>(type: K, payload: BusEventMap[K]): void;
  publish(type: string, payload: unknown): void;
  publish(type: string, payload: unknown): void {
    const set = this.subs.get(type);
    if (!set) return;
    // Snapshot to insulate against handlers that subscribe/unsubscribe
    // during dispatch.
    for (const h of [...set]) {
      try {
        h(payload);
      } catch (err) {
        console.error("[eventBus] handler threw for", type, err);
      }
    }
  }
}

export const eventBus = new EventBus();

/** Convenience: subscribe across multiple types, return a single
 * unsubscribe that detaches all of them. Used by sessionRegistry to
 * wire up its delta listeners in one call. */
export function subscribeMany(
  pairs: Array<[type: string, handler: Handler<unknown>]>,
): () => void {
  const offs = pairs.map(([t, h]) => eventBus.subscribe(t, h));
  return () => {
    for (const off of offs) off();
  };
}
