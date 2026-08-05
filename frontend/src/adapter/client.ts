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
  Focus,
  NodeWire,
  OlderEnvelope,
  SearchEnvelope,
  SnapshotEnvelope,
  SurfaceCursor,
  SurfaceSubscribeMessage,
} from "./wire";

const _REST_PREFIX = "/api/v2/surface";

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
  private readonly handlers: SurfaceSocketHandlers;

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

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
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
      if (!parsed || typeof parsed !== "object") return;
      const frame = parsed as ChatFrame;
      if (frame.type === "resync_required") {
        this.handlers.onResyncRequired(frame.surface_id);
        return;
      }
      this.handlers.onFrame(frame);
    };
    ws.onclose = () => {
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
