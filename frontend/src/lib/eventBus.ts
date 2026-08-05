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
 *
 * A few facts are CLIENT-LOCAL rather than backend frames — they have
 * no backend `WSEventType` counterpart and are published by frontend
 * code about frontend state (see `ws_connection_changed`). They ride
 * the same bus so any consumer can subscribe by type; they are marked
 * as client-local in `BusEventMap`.
 */

import type {
  NodeRegistrationResolvedData,
  NodeProviderCredentialsChangedData,
  NodeStateChangedData,
  PendingApproval,
  PendingNodeRegistration,
  ProjectAggregatesChangedData,
  Schedule,
  Session,
  SessionStatusKey,
  StartupTask,
  BackgroundWorkItem,
  ToolApproval,
  UserInteractionRequest,
} from "../types";

// Map of event type → payload shape. Only events that have a typed
// consumer need to be enumerated here; everything else falls back to
// `unknown` via the generic publish/subscribe overload.
export interface BusEventMap {
  // CLIENT-LOCAL fact (no backend counterpart): the app's single
  // WebSocket opened or closed. Published by `useWebSocket`. Consumers
  // that mirror backend state from a REST snapshot subscribe to it and
  // re-pull on `connected: true`, because cross-session pings are
  // fire-and-forget (`coordinator.broadcast_global`) and are NOT
  // replayed on reconnect — anything broadcast during a disconnect
  // window is lost to this client. A FACT, not a command: each owner
  // decides whether it needs to rehydrate.
  ws_connection_changed: {
    connected: boolean;
  };
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
    // `sound_setting` names a boolean setting of the owning extension that
    // gates the sound; absent means the sound is ungated.
    marker: {
      color: string;
      tooltip: string;
      sound?: boolean;
      sound_setting?: string;
    } | null;
  };
  // Authoritative per-session monitoring state (active / idle /
  // blocked_on_user / waiting_on_background / stopped). The registry derives
  // the Running and Waiting dimensions from this one backend-owned fact.
  session_monitoring_changed: {
    session_id: string;
    monitoring_state: string;
  };
  // A provenance row was appended; the open Details panel refetches.
  session_provenance_changed: {
    session_id: string;
  };
  node_state_changed: NodeStateChangedData;
  node_provider_credentials_changed: NodeProviderCredentialsChangedData;
  node_registration_requested: PendingNodeRegistration;
  node_registration_resolved: NodeRegistrationResolvedData;
  user_input_requested: UserInteractionRequest;
  user_input_resolved: {
    request_id: string;
    app_session_id?: string;
  };
  tool_approval_requested: ToolApproval;
  tool_approval_resolved: {
    approval_id: string;
    app_session_id?: string;
  };
  worker_creation_requested: PendingApproval;
  worker_creation_approved: {
    delegation_id: string;
  };
  worker_creation_failed: {
    delegation_id: string;
    error?: string;
  };
  provider_changed: Record<string, unknown>;
  installation_capabilities_changed: Record<string, unknown>;
  provider_install_progress:
    | { kind: string; phase: "started" }
    | { kind: string; stream: "stdout" | "stderr"; text: string };
  provider_install_finished: {
    kind: string;
    label: string;
    command: string;
    state: "running" | "succeeded" | "failed";
    lines: Array<{ s: "stdout" | "stderr"; t: string }>;
    started_at: string | null;
    finished_at: string | null;
    returncode: number | null;
    installed: boolean | null;
    message: string | null;
  };
  models_catalog_changed: {
    provider_id: string;
    kind?: string;
    idempotency_key?: string;
    projection?: unknown;
    newly_added?: string[];
    became_active?: string[];
    went_retired?: string[];
    truly_removed?: string[];
  };
  startup_task_changed:
    | { cleared: true }
    | { task: StartupTask };
  background_work_changed:
    | { epoch: string; cleared: true }
    | { epoch: string; item: BackgroundWorkItem }
    | { epoch: string; seq: number; removed_id: string };
  // Pre-existing frames the registry cares about — listed so the
  // typed dispatch helpers in `useWebSocket` can `eventBus.publish(...)`
  // them with payload safety.
  projects_changed: Record<string, unknown>;
  project_aggregates_changed: ProjectAggregatesChangedData;
  session_status_changed: {
    session_id: string;
    status_key: SessionStatusKey;
    status_rank: number;
  };
  session_created: { session: Session };
  session_deleted: { session_id: string };
  session_renamed: { session_id: string; name: string };
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
  // Global ping — any schedule mutated; refetch GET /api/schedules.
  schedules_changed: Record<string, unknown>;
  "extension.catalog": Record<string, unknown>;
  "extension.config": Record<string, unknown>;
  "extension.config.settings": Record<string, unknown>;
  "extension.config.instructions": Record<string, unknown>;
  "extension.config.ui_settings": Record<string, unknown>;
  "extension.config.internal_llm": Record<string, unknown>;
  "extension.config.permissions": Record<string, unknown>;
  "extension.config.mcp": Record<string, unknown>;
  "extension.config.skills": Record<string, unknown>;
  "extension.ui": Record<string, unknown>;
  "extension.ui.frontend_modules": Record<string, unknown>;
  "extension.harness.default": Record<string, unknown>;
  "extension.harness.default.disabled_builtin_extensions": Record<string, unknown>;
  "extension.harness.default.disabled_builtin_tools": Record<string, unknown>;
  harness_profiles_changed: Record<string, unknown>;
  // Remote-extension update projection changed; refetch GET /api/extensions/updates.
  extension_updates_changed: {
    available: string[];
  };
  marketplace_bridge_changed: Record<string, unknown>;
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
}

type Handler<T> = (payload: T) => void;

function topicAncestors(type: string): string[] {
  if (!type.includes(".")) return [type];
  const parts = type.split(".");
  const topics: string[] = [];
  for (let i = 1; i <= parts.length; i += 1) {
    topics.push(parts.slice(0, i).join("."));
  }
  return topics;
}

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
    const handlers = new Set<Handler<unknown>>();
    for (const topic of topicAncestors(type)) {
      const set = this.subs.get(topic);
      if (!set) continue;
      for (const handler of set) handlers.add(handler);
    }
    if (!handlers.size) return;
    // Snapshot to insulate against handlers that subscribe/unsubscribe
    // during dispatch.
    for (const h of [...handlers]) {
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
