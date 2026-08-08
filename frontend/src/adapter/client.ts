// Transport for the Chat Surface Contract v2 (backend/adapter_api.py).
//
// Reuses the app's existing auth mechanics verbatim — no new auth path:
//   - REST: `fetch(..., { credentials: "include" })`, exactly like every
//     other typed helper in `frontend/src/api.ts`. The bearer-auth
//     interceptor installed in main.tsx (frontend/src/bearerAuth.ts)
//     patches `window.fetch` globally, so the Authorization header is
//     attached automatically wherever it's needed (native/cross-site).
//   - WS: `withTokenQuery` (frontend/src/bearerAuth.ts) appends `?token=`
//     in the same cookie-blocked contexts `getWsUrl()` (frontend/src/api.ts)
//     does for the legacy `/ws/chat` socket.

import { API } from "../api";
import { withTokenQuery } from "../bearerAuth";
import type {
  ChatFrame,
  ChildrenEnvelope,
  ExtensionCatalogEnvelope,
  ExtensionConfigEnvelope,
  ExtensionConfigPageEnvelope,
  ExtensionUiEnvelope,
  FeedName,
  FoldersEnvelope,
  Focus,
  HarnessProfilesEnvelope,
  HostStartupTasksEnvelope,
  InstallableListEnvelope,
  InstallationCapabilitiesEnvelope,
  MachinesEnvelope,
  MarketplaceBridgeEnvelope,
  MarketplaceIntentsEnvelope,
  ModelCatalogEnvelope,
  NodeProviderCredentialsEnvelope,
  NodeRegistrationsEnvelope,
  NodeWire,
  OlderEnvelope,
  ProjectsEnvelope,
  ProviderFrame,
  ProviderListEnvelope,
  RunDetailEnvelope,
  RunsEnvelope,
  RunsFrame,
  RuntimeProfilesSnapshotEnvelope,
  SchedulesEnvelope,
  SearchEnvelope,
  SessionsEnvelope,
  SessionsFrame,
  SnapshotEnvelope,
  SurfaceCursor,
  SurfaceIntentWire,
  SurfaceSubscribeMessage,
  SystemFrame,
  TagsEnvelope,
  TransportAckFrame,
} from "./wire";
import { isProviderFrame, isSessionsFrame, isSystemFrame, isTransportAckFrame } from "./wire";

const _REST_PREFIX = "/api/v2/surface";

/** URL for one `Attachment.ref`'s bytes (adapter_api.py's
 * `/attachments/{ref}` route). Plain string, no fetch — callers hand it
 * straight to `<img src>` / lazy `fetch`, same as the legacy
 * `buildMessageImageUrl` (frontend/src/utils/messageImages.ts): same-origin
 * cookie auth covers it, so no bearer token is appended here either. */
export function attachmentUrl(sessionId: string, ref: string): string {
  if (!sessionId || !ref) return "";
  return `${API}${_REST_PREFIX}/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(ref)}`;
}

function _wsBase(): string {
  return API
    ? API.replace(/^http/, "ws")
    : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
}

/** Resolved fresh on every (re)connect, same rationale as `getWsUrl()` in
 * frontend/src/api.ts — a token acquired after module load must still
 * make it onto the handshake URL. */
function surfaceWsUrl(): string {
  return withTokenQuery(`${_wsBase()}/ws/v2/surface`);
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
  return (await res.json()) as T;
}

export class SurfaceClient {
  fetchSnapshot(sessionId: string): Promise<SnapshotEnvelope> {
    return getJson<SnapshotEnvelope>(
      `${API}${_REST_PREFIX}/sessions/${encodeURIComponent(sessionId)}/snapshot`,
    );
  }

  fetchChildren(
    sessionId: string,
    nodeId: string,
    atRenderRev: number,
  ): Promise<ChildrenEnvelope> {
    const qs = new URLSearchParams({ at_render_rev: String(atRenderRev) });
    return getJson<ChildrenEnvelope>(
      `${API}${_REST_PREFIX}/sessions/${encodeURIComponent(sessionId)}/nodes/${encodeURIComponent(nodeId)}/children?${qs}`,
    );
  }

  fetchOlder(sessionId: string, cursorToken: string): Promise<OlderEnvelope> {
    const qs = new URLSearchParams({ cursor: cursorToken });
    return getJson<OlderEnvelope>(
      `${API}${_REST_PREFIX}/sessions/${encodeURIComponent(sessionId)}/older?${qs}`,
    );
  }

  search(sessionId: string, query: string): Promise<SearchEnvelope> {
    const qs = new URLSearchParams({ q: query });
    return getJson<SearchEnvelope>(
      `${API}${_REST_PREFIX}/sessions/${encodeURIComponent(sessionId)}/search?${qs}`,
    );
  }

  /** ADR 0009 (runs & diagnostics): `GET /runs[?session_id=&cursor=]` —
   * `sessionId` omitted lists every run visible to this connection's
   * auth, matching `adapter_api.list_runs`'s own optional query param. */
  fetchRuns(sessionId?: string, cursorToken?: string): Promise<RunsEnvelope> {
    const qs = new URLSearchParams();
    if (sessionId) qs.set("session_id", sessionId);
    if (cursorToken) qs.set("cursor", cursorToken);
    const query = qs.toString();
    return getJson<RunsEnvelope>(`${API}${_REST_PREFIX}/runs${query ? `?${query}` : ""}`);
  }

  /** ADR 0009: `GET /runs/{run_id}` — typed `RunDetailEntry[]` (key/
   * value_kind/value), generic by design so a new backend-added entry
   * key renders with zero client changes (see RunDetailsModal.tsx). */
  fetchRunDetail(runId: string): Promise<RunDetailEnvelope> {
    return getJson<RunDetailEnvelope>(`${API}${_REST_PREFIX}/runs/${encodeURIComponent(runId)}`);
  }

  /** ADR 0007 (provider config & auth, Package D): `GET /providers`. */
  fetchProviders(): Promise<ProviderListEnvelope> {
    return getJson<ProviderListEnvelope>(`${API}${_REST_PREFIX}/providers`);
  }

  /** D2: `GET /providers/installable` — the backend-owned catalog of
   * provider kinds that CAN be configured, replacing the frontend's static
   * template list. */
  fetchInstallableProviders(): Promise<InstallableListEnvelope> {
    return getJson<InstallableListEnvelope>(`${API}${_REST_PREFIX}/providers/installable`);
  }

  fetchProviderModelCatalog(providerId: string): Promise<ModelCatalogEnvelope> {
    return getJson<ModelCatalogEnvelope>(
      `${API}${_REST_PREFIX}/providers/${encodeURIComponent(providerId)}/models`,
    );
  }

  /** Named `...Descriptors` (not `fetchRuntimeProfiles`, already taken by
   * the legacy `api.ts` helper returning the frontend's own
   * `RuntimeProfilesSnapshot` type, `../types.ts` — structurally the same
   * fields as this v2 route now returns (closure 3), but a distinct type
   * on the TS side, kept so the two call sites stay visibly distinct at
   * every import site) — full snapshot, not just a bare profile list. */
  fetchRuntimeProfileDescriptors(): Promise<RuntimeProfilesSnapshotEnvelope> {
    return getJson<RuntimeProfilesSnapshotEnvelope>(`${API}${_REST_PREFIX}/runtime-profiles`);
  }

  /** ADR 0008 (session/project/folder/tag surface, Package B): `GET
   * /sessions[?cursor=&q=&folder_ref=&tag_ref=&tag_match=]` — one query
   * path (folder/tag filters ride the same list, no parallel filtered-list
   * route, matching `adapter_api.py`'s `list_sessions`). `tagRefs`
   * repeats `?tag_ref=` per entry (FastAPI `Query(default=[])`). */
  fetchSessions(params?: {
    cursor?: string;
    q?: string;
    folderRef?: string;
    tagRefs?: string[];
    tagMatch?: "all" | "any";
  }): Promise<SessionsEnvelope> {
    const qs = new URLSearchParams();
    if (params?.cursor) qs.set("cursor", params.cursor);
    if (params?.q) qs.set("q", params.q);
    if (params?.folderRef) qs.set("folder_ref", params.folderRef);
    for (const tagRef of params?.tagRefs ?? []) qs.append("tag_ref", tagRef);
    if (params?.tagMatch) qs.set("tag_match", params.tagMatch);
    const query = qs.toString();
    return getJson<SessionsEnvelope>(`${API}${_REST_PREFIX}/sessions${query ? `?${query}` : ""}`);
  }

  /** ADR 0008: `GET /projects` — real `session_count`/persisted `order`
   * (Package B's B1), no rearranger concept (removed). */
  fetchProjectsV2(): Promise<ProjectsEnvelope> {
    return getJson<ProjectsEnvelope>(`${API}${_REST_PREFIX}/projects`);
  }

  /** ADR 0008: `GET /folders[?project_ref=]` — bounded, not paged (small
   * per-project catalogs). Omitting `project_ref` returns every folder
   * visible to this connection's auth. */
  fetchFolders(projectRef?: string): Promise<FoldersEnvelope> {
    const qs = projectRef ? `?project_ref=${encodeURIComponent(projectRef)}` : "";
    return getJson<FoldersEnvelope>(`${API}${_REST_PREFIX}/folders${qs}`);
  }

  /** ADR 0008: `GET /tags[?project_ref=]` — same bounded-catalog contract
   * as `fetchFolders`; a tag with `project_ref: null` is global. */
  fetchTags(projectRef?: string): Promise<TagsEnvelope> {
    const qs = projectRef ? `?project_ref=${encodeURIComponent(projectRef)}` : "";
    return getJson<TagsEnvelope>(`${API}${_REST_PREFIX}/tags${qs}`);
  }

  // ---- ADR 0011 (System & Host Surface, Package C) — 13 read routes ----

  fetchExtensionConfig(extensionId: string): Promise<ExtensionConfigEnvelope> {
    return getJson<ExtensionConfigEnvelope>(
      `${API}${_REST_PREFIX}/extension-config/${encodeURIComponent(extensionId)}`,
    );
  }

  fetchExtensionConfigs(cursorToken?: string): Promise<ExtensionConfigPageEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<ExtensionConfigPageEnvelope>(`${API}${_REST_PREFIX}/extension-config${qs}`);
  }

  fetchHarnessProfiles(cursorToken?: string): Promise<HarnessProfilesEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<HarnessProfilesEnvelope>(`${API}${_REST_PREFIX}/harness-profiles${qs}`);
  }

  fetchExtensionUi(cursorToken?: string): Promise<ExtensionUiEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<ExtensionUiEnvelope>(`${API}${_REST_PREFIX}/extension-ui${qs}`);
  }

  fetchExtensionCatalog(cursorToken?: string): Promise<ExtensionCatalogEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<ExtensionCatalogEnvelope>(`${API}${_REST_PREFIX}/extension-catalog${qs}`);
  }

  fetchMarketplaceBridge(): Promise<MarketplaceBridgeEnvelope> {
    return getJson<MarketplaceBridgeEnvelope>(`${API}${_REST_PREFIX}/marketplace-bridge`);
  }

  fetchMarketplaceIntents(cursorToken?: string): Promise<MarketplaceIntentsEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<MarketplaceIntentsEnvelope>(`${API}${_REST_PREFIX}/marketplace-intents${qs}`);
  }

  /** Omitting `sessionId` returns the cross-session list (matches the
   * legacy global `schedules_changed` refetch — ADR 0011 §6). */
  fetchSchedules(sessionId?: string, cursorToken?: string): Promise<SchedulesEnvelope> {
    const qs = new URLSearchParams();
    if (sessionId) qs.set("session_id", sessionId);
    if (cursorToken) qs.set("cursor", cursorToken);
    const query = qs.toString();
    return getJson<SchedulesEnvelope>(`${API}${_REST_PREFIX}/schedules${query ? `?${query}` : ""}`);
  }

  fetchHostStartupTasks(): Promise<HostStartupTasksEnvelope> {
    return getJson<HostStartupTasksEnvelope>(`${API}${_REST_PREFIX}/host-startup-tasks`);
  }

  fetchInstallationCapabilities(): Promise<InstallationCapabilitiesEnvelope> {
    return getJson<InstallationCapabilitiesEnvelope>(`${API}${_REST_PREFIX}/installation-capabilities`);
  }

  fetchMachines(cursorToken?: string): Promise<MachinesEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<MachinesEnvelope>(`${API}${_REST_PREFIX}/machines${qs}`);
  }

  fetchNodeProviderCredentials(nodeId: string): Promise<NodeProviderCredentialsEnvelope> {
    return getJson<NodeProviderCredentialsEnvelope>(
      `${API}${_REST_PREFIX}/machines/${encodeURIComponent(nodeId)}/provider-credentials`,
    );
  }

  fetchNodeRegistrations(cursorToken?: string): Promise<NodeRegistrationsEnvelope> {
    const qs = cursorToken ? `?cursor=${encodeURIComponent(cursorToken)}` : "";
    return getJson<NodeRegistrationsEnvelope>(`${API}${_REST_PREFIX}/node-registrations${qs}`);
  }

  /** Recursively resolves one turn's full body content. `children(turn)`
   * returns only the top-level derived items (Explanation wrapper nodes
   * plus any preserved-in-place SubAgentTurn/SteeringMessage nodes —
   * backend/adapters/derive.py `derive_body`); an Explanation's actual
   * members (thinking/tool_interaction/assistant_text/...) are one more
   * `children(explanation_id)` call away. Explanation nodes themselves
   * carry no payload (`_NON_RENDERABLE_KINDS`) and are dropped here —
   * mapToRenderModel.ts only ever sees real content nodes, flat and
   * ordered. Skips the round trip entirely when the turn's manifest
   * already reports zero renderable children. */
  async fetchTurnBody(
    sessionId: string,
    turnNodeId: string,
    atRenderRev: number,
  ): Promise<NodeWire[]> {
    const top = await this.fetchChildren(sessionId, turnNodeId, atRenderRev);
    if (top.kind !== "ok") return [];
    const out: NodeWire[] = [];
    for (const node of top.value) {
      if (node.kind !== "explanation") {
        out.push(node);
        continue;
      }
      const manifest = node.child_manifest;
      if (manifest && manifest.renderable_child_count === 0) continue;
      const members = await this.fetchChildren(sessionId, node.node_id, atRenderRev);
      if (members.kind === "ok") out.push(...members.value);
    }
    return out.sort((a, b) => a.ts - b.ts || a.seq - b.seq);
  }

  openSocket(handlers: SurfaceSocketHandlers): SurfaceSocket {
    return new SurfaceSocket(handlers);
  }
}

export interface SurfaceSocketHandlers {
  onFrame: (frame: ChatFrame) => void;
  /** Backend's `incarnation` moved out from under a subscribed cursor
   * (chat_adapter.py `subscribe`) — the only correct response is a full
   * snapshot refetch, never a silently-degraded partial replay. */
  onResyncRequired: (surfaceId: string) => void;
  /** Non-cursor live feed push (`{"feeds": [...]}`, see `setFeeds` below)
   * — `run_summary_upsert` today. Not `ChatFrame`-shaped (no `surface_id`/
   * `snapshot`), so it is dispatched separately from `onFrame` rather than
   * folded into that union. Optional: a caller that never calls
   * `setFeeds` never receives one. */
  onRunsFrame?: (frame: RunsFrame) => void;
  /** Non-cursor live feed push (`{"feeds": ["providers"]}`) — ADR 0007's
   * `provider_upsert`/`credential_state`/`model_catalog_changed`/
   * `login_flow_frame`/`installable_catalog_changed`. Same split rationale
   * as `onRunsFrame` above (no `surface_id`/`snapshot`). Optional: a
   * caller that never calls `setFeeds(["providers"])` never receives one. */
  onProviderFrame?: (frame: ProviderFrame) => void;
  /** Non-cursor live feed push (`{"feeds": ["sessions"]}`) — ADR 0008's
   * `session_summary_upsert`/`session_tree_changed`/`project_upsert`/
   * `session_removed`/`project_removed`/`folder_upsert`/`folder_removed`/
   * `tag_upsert`/`tag_removed`, all pushed under the ONE `"sessions"` feed
   * name (`adapter_api.py`'s `_feed_surface` maps it straight onto
   * `SessionSurfaceAdapter.subscribe`, unfiltered). Same split rationale as
   * `onRunsFrame`/`onProviderFrame` above. Optional: a caller that never
   * calls `setFeeds(["sessions"])` never receives one. */
  onSessionsFrame?: (frame: SessionsFrame) => void;
  /** Non-cursor live feed push (ADR 0011's 12 system feeds —
   * `extension_notices`/`extension_config`/`harness_profiles`/
   * `extension_ui`/`extension_catalog`/`marketplace_bridge`/
   * `marketplace_intents`/`schedules`/`host_startup_tasks`/
   * `installation_capabilities`/`machines`/`node_registrations`). Same
   * split rationale as `onRunsFrame`/`onProviderFrame`/`onSessionsFrame`
   * above. Optional: a caller that never calls `setFeeds([...system feed
   * names])` never receives one. */
  onSystemFrame?: (frame: SystemFrame) => void;
  /** A late, unmatched `intent_rejected` — the synchronous ack for its
   * `intent_id` already resolved (as `intent_accepted`) via `submit()`'s
   * own promise, so this is the only way a caller learns a fire-and-forget
   * intent's async business-logic failure (D4's provider-plane pattern,
   * generalized here for any intent surface — see the `onmessage` unmatched-
   * ack branch below). Additive: pre-existing `onProviderFrame` late-
   * rejection forwarding is unchanged; this is a second, surface-agnostic
   * delivery of the SAME frame so a session-intent caller (`lib/
   * sessionSurfaceRegistry.ts`) doesn't have to depend on the provider
   * plane's handler. */
  onLateIntentRejected?: (frame: TransportAckFrame) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

const RECONNECT_DELAY_MS = 2000;

/** One `/ws/v2/surface` connection: opens, sends the current cursor set,
 * and re-sends it (with whatever the caller last set via `updateCursors`)
 * on every reconnect — mirrors the reconnect/cursor-resend behavior of
 * the legacy `/ws/chat` socket in frontend/src/hooks/useWebSocket.ts. */
export class SurfaceSocket {
  private ws: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private cursors: SurfaceCursor[] = [];
  private focus: Focus = "opened";
  /** Non-cursor live feeds this connection wants (`{"feeds": [...]}`,
   * adapter_api.py's `_FEED_SURFACES`). Empty until a caller opts in via
   * `setFeeds` — an untouched socket (e.g. `SurfaceStore` in
   * `surface/state.ts`) never sends a `feeds` message at all, so this adds
   * zero wire chatter for every pre-existing consumer. */
  private feeds: FeedName[] = [];
  private readonly handlers: SurfaceSocketHandlers;
  /** intent_id -> resolver, for `submit()`'s ack matching. Settled (never
   * left dangling) by exactly one of: the matching ack frame arriving
   * (`ws.onmessage`), or the connection closing (`ws.onclose`) — no
   * timeout, since both are real completion events on this connection and
   * a reconnect always starts a fresh, empty map (a submit issued before a
   * drop can never be acked by a later, different connection). */
  private pendingAcks = new Map<string, (ack: TransportAckFrame) => void>();

  constructor(handlers: SurfaceSocketHandlers) {
    this.handlers = handlers;
  }

  open(cursors: SurfaceCursor[], focus: Focus = "opened"): void {
    this.cursors = cursors;
    this.focus = focus;
    this.closed = false;
    this.connect();
  }

  /** Update the subscribed cursor set and resend immediately over the
   * open connection (e.g. after a fresh hydration following
   * `resync_required`). No-ops (queues for the next connect) when not
   * currently open. */
  updateCursors(cursors: SurfaceCursor[], focus: Focus = this.focus): void {
    this.cursors = cursors;
    this.focus = focus;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.send();
    }
  }

  /** Record the freshest cursor WITHOUT resending on the current
   * connection — resending on every live frame would thrash the
   * backend's per-connection subscription (chat_adapter.py's `subscribe`
   * closes and re-registers on every `{"surfaces": [...]}` message). Only
   * used so a FUTURE reconnect (network blip) resumes from the latest
   * point instead of the stale point from the last explicit `open`/
   * `updateCursors` call. */
  trackCursor(cursors: SurfaceCursor[]): void {
    this.cursors = cursors;
  }

  /** Sets the non-cursor live-feed subscription set and, when currently
   * OPEN, resends immediately (mirrors `updateCursors`'s resend-now
   * contract). Also the value replayed on every future reconnect (see
   * `connect()`), same as `cursors`/`focus`. Passing `[]` explicitly
   * unsubscribes from every feed on this connection. */
  setFeeds(feeds: FeedName[]): void {
    this.feeds = feeds;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.sendFeeds();
    }
  }

  private sendFeeds(): void {
    if (!this.ws) return;
    this.ws.send(JSON.stringify({ feeds: this.feeds }));
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.rejectPendingAcks("connection closed");
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.close();
    }
  }

  /** Submits a command intent (adapter_api.py's `{"intent": {...}}` WS
   * message) over THIS connection. Same not-open contract as the legacy
   * `/ws/chat` socket's `sendMessage` (useAppWebSocket.ts): returns `null`
   * synchronously, without queuing, when the socket isn't OPEN — the
   * caller decides how to handle that (SurfaceStore.sendPrompt flips the
   * optimistic entry to an error state; there is no client-side
   * queue-until-open here, matching legacy's own behavior exactly). When
   * open, resolves with the backend's IntentAccepted/IntentRejected ack,
   * matched by `intent_id` — no timeout: `_handle_intent` always answers
   * synchronously on the same connection, and a connection drop before an
   * answer rejects the promise via `rejectPendingAcks` instead of hanging.
   *
   * Accepts `SurfaceIntentWire` (`ChatIntentWire | ProviderIntentWire`) — a
   * pure type widening over the original `ChatIntentWire`-only signature,
   * backward compatible for every existing chat call site — so the
   * provider plane (Package D) submits `ProviderIntent`s over this SAME
   * connection/ack-matching path instead of a second implementation. */
  submit(intent: SurfaceIntentWire): Promise<TransportAckFrame> | null {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return null;
    const ws = this.ws;
    return new Promise<TransportAckFrame>((resolve, reject) => {
      this.pendingAcks.set(intent.intent_id, resolve);
      try {
        ws.send(JSON.stringify({ intent }));
      } catch (err) {
        this.pendingAcks.delete(intent.intent_id);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  private rejectPendingAcks(reason: string): void {
    if (this.pendingAcks.size === 0) return;
    const pending = this.pendingAcks;
    this.pendingAcks = new Map();
    for (const [intentId] of pending) {
      // Resolved as an IntentRejected, not a thrown rejection: a dropped
      // connection is a normal, expected outcome a caller handles the same
      // way as a backend-issued rejection (SurfaceStore.sendPrompt's
      // `.then` has one branch, not a `.catch`) — reserving an actual
      // Promise rejection for the truly exceptional synchronous-throw case
      // in `submit()` above.
      pending.get(intentId)!({
        type: "intent_rejected",
        intent_id: intentId,
        code: "connection_lost",
        message: reason,
      });
    }
  }

  private send(): void {
    if (!this.ws || this.cursors.length === 0) return;
    const message: SurfaceSubscribeMessage = { surfaces: this.cursors, focus: this.focus };
    this.ws.send(JSON.stringify(message));
  }

  private connect(): void {
    if (this.closed) return;
    const ws = new WebSocket(surfaceWsUrl());
    ws.onopen = () => {
      this.send();
      // Only replayed when a caller has actually opted in (see `setFeeds`'s
      // docstring) — an untouched socket never sends `{"feeds": []}`.
      if (this.feeds.length > 0) this.sendFeeds();
      this.handlers.onOpen?.();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (!parsed || typeof parsed !== "object" || !("type" in parsed)) return;
      const typed = parsed as { type: string };
      if (isTransportAckFrame(typed)) {
        const resolve = this.pendingAcks.get(typed.intent_id);
        if (resolve) {
          this.pendingAcks.delete(typed.intent_id);
          resolve(typed);
          return;
        }
        // No `submit()` call is still waiting on this `intent_id` — an
        // already-resolved (accepted) provider mutation whose fire-and-
        // forget coroutine has now failed (adapter_api.py's
        // `ProviderCommandPort` -> `provider_adapter.py`'s
        // `reject_intent`, D4's async intent-error-propagation fix). Only
        // `intent_rejected` is meaningful this late (a late, unmatched
        // `intent_accepted` has nothing further to say); forward it as a
        // `ProviderFrame` so a `subscribeProviderFrames`/`onProviderFrame`
        // listener can surface the failure instead of it being dropped.
        if (typed.type === "intent_rejected") {
          this.handlers.onProviderFrame?.(typed as unknown as ProviderFrame);
          // Surface-agnostic delivery of the SAME late rejection (see
          // `onLateIntentRejected`'s docstring) — a session-intent caller
          // (`lib/sessionSurfaceRegistry.ts`) doesn't have to depend on the
          // provider plane's handler to learn its own fire-and-forget
          // intent's async failure.
          this.handlers.onLateIntentRejected?.(typed as unknown as TransportAckFrame);
        }
        return;
      }
      if (typed.type === "run_summary_upsert") {
        this.handlers.onRunsFrame?.(parsed as RunsFrame);
        return;
      }
      if (isProviderFrame(typed)) {
        this.handlers.onProviderFrame?.(parsed as ProviderFrame);
        return;
      }
      if (isSessionsFrame(typed)) {
        this.handlers.onSessionsFrame?.(parsed as SessionsFrame);
        return;
      }
      // Checked before the generic ChatFrame fallback below.
      if (isSystemFrame(typed)) {
        this.handlers.onSystemFrame?.(parsed as SystemFrame);
        return;
      }
      const frame = parsed as ChatFrame;
      if (frame.type === "resync_required") {
        this.handlers.onResyncRequired(frame.surface_id);
        return;
      }
      this.handlers.onFrame(frame);
    };
    ws.onclose = () => {
      this.rejectPendingAcks("connection closed");
      this.handlers.onClose?.();
      if (!this.closed) {
        this.reconnectTimer = setTimeout(() => this.connect(), RECONNECT_DELAY_MS);
      }
    };
    ws.onerror = () => {
      ws.close();
    };
    this.ws = ws;
  }
}
