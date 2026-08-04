import { useState, useRef, useCallback, useEffect } from "react";
import type { WSEvent } from "../types";
import { eventBus } from "../lib/eventBus";
import { getWsUrl } from "../api";
import { SnapshotTransport } from "../lib/snapshotTransport";
import { SNAPSHOT_BINARY_SUBPROTOCOL } from "../lib/snapshotBinary";
import { logFailure, logTiming } from "../lib/frontendLogger";
import {
  sendWebSocketFrame,
  webSocketDataBytes,
  webSocketTrafficLog,
} from "../lib/webSocketTrafficLog";
import { parseWireEvent } from "../lib/webSocketIngress";

// Transport liveness check. Neither the ASGI server nor any
// reverse proxy in this stack configures a WS idle timeout, and a mobile
// network transition (OS suspends background sockets, WiFi<->cellular
// handoff, carrier NAT idle-drop) can kill the underlying TCP connection
// without either end ever seeing a close/error frame -- `readyState` then
// reports OPEN forever while nothing arrives. A ping/pong heartbeat is the
// only way to detect that from JS; the interval and timeout below tolerate
// one missed round trip before treating the connection as dead.
const HEARTBEAT_INTERVAL_MS = 15_000;
const HEARTBEAT_TIMEOUT_MS = 35_000;

export interface UseWebSocketOptions {
  currentAppSessionId?: string | null;
  prepareSessionSubscriptions?: () => void | Promise<void>;
  additionalAppSessionIds?: string[];
  getSinceSeq?: (appSessionId: string | null) => number;
  getEventsFromSeq?: (appSessionId: string | null) => number;
  getEventsCursorKnown?: (appSessionId: string | null) => boolean;
  onEventSeqAdvance?: (appSessionId: string, seq: number) => void;
  onBeforePublish?: (event: WSEvent) => void;
  onEvent?: (event: WSEvent) => void | Promise<void>;
  onConnectionChanged?: (connected: boolean) => void;
}

export interface WebSocketFrame {
  type: string;
  [key: string]: unknown;
}

export interface UseWebSocketReturn {
  connected: boolean;
  checkConnection: () => void;
  getReadyState: () => number;
  send: (frame: WebSocketFrame) => boolean;
  subscribeSession: (appSessionId: string) => boolean;
}

export function useWebSocket(
  options: UseWebSocketOptions = {}
): UseWebSocketReturn {
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  }, [options]);
  const [connected, setConnected] = useState(false);
  const [subscriptionsReady, setSubscriptionsReady] = useState(true);
  const hasOpenedRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const snapshotTransportRef = useRef(new SnapshotTransport());
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const lastPongAtRef = useRef(0);
  const heartbeatIntervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const connectRef = useRef<() => void>(() => undefined);
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    // Re-resolve via getWsUrl so a bearer token acquired AFTER module
    // load (right after login on native) ends up on the handshake URL.
    // Browser path is the same URL, just without a ?token= suffix.
    const ws = new WebSocket(getWsUrl(), SNAPSHOT_BINARY_SUBPROTOCOL);
    ws.binaryType = "arraybuffer";
    let routingVerifiedSnapshot = false;
    let verifiedRouteResult: void | Promise<void>;

    const routeVerifiedSnapshot = (event: WSEvent) => {
      routingVerifiedSnapshot = true;
      verifiedRouteResult = undefined;
      try {
        ws.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
        return verifiedRouteResult;
      } finally {
        routingVerifiedSnapshot = false;
      }
    };

    ws.onopen = () => {
      const isReconnect = hasOpenedRef.current;
      hasOpenedRef.current = true;
      setConnected(true);
      try {
        optionsRef.current.onConnectionChanged?.(true);
      } catch {
        // Connection observers cannot interrupt transport setup.
      }
      eventBus.publish("ws_connection_changed", { connected: true });
      snapshotTransportRef.current.resume((frame) => sendWebSocketFrame(ws, frame));
      if (isReconnect && optionsRef.current.prepareSessionSubscriptions) {
        setSubscriptionsReady(false);
        void Promise.resolve(optionsRef.current.prepareSessionSubscriptions()).then(() => {
          if (wsRef.current === ws && ws.readyState === WebSocket.OPEN) {
            setSubscriptionsReady(true);
          }
        });
      } else {
        setSubscriptionsReady(true);
      }
      lastPongAtRef.current = Date.now();
      heartbeatIntervalRef.current = setInterval(() => {
        if (Date.now() - lastPongAtRef.current > HEARTBEAT_TIMEOUT_MS) {
          // No pong for two heartbeats straight: the connection is a
          // zombie (readyState still OPEN, nothing actually arriving).
          // Force a close so the existing onclose->reconnect path fires
          // -- do not invent a second reconnection code path.
          ws.close();
          return;
        }
        sendWebSocketFrame(ws, { type: "ping" });
      }, HEARTBEAT_INTERVAL_MS);
    };

    ws.onclose = (ev) => {
      webSocketTrafficLog.flush("connection_closed");
      if (heartbeatIntervalRef.current !== undefined) {
        clearInterval(heartbeatIntervalRef.current);
        heartbeatIntervalRef.current = undefined;
      }
      setConnected(false);
      setSubscriptionsReady(false);
      try {
        optionsRef.current.onConnectionChanged?.(false);
      } catch {
        // Connection observers cannot interrupt transport cleanup.
      }
      eventBus.publish("ws_connection_changed", { connected: false });
      // Code 1008 ("policy violation") is what the backend sends from
      // /ws/chat when the better_agent_session cookie is missing or invalid. Tell
      // the top-level App to swap to <Login /> instead of reconnecting
      // forever against a 401 wall.
      if (ev.code === 1008) {
        window.dispatchEvent(new CustomEvent("better-agent-auth-failed"));
        return;
      }
      reconnectTimer.current = setTimeout(() => connectRef.current(), 2000);
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onmessage = (e) => {
      const frameStartedAt = performance.now();
      let eventType = "unknown";
      let frameFailed = false;
      const byteSize = webSocketDataBytes(e.data);
      try {
        if (e.data instanceof ArrayBuffer) {
          eventType = "snapshot_chunk";
          if (!routingVerifiedSnapshot && snapshotTransportRef.current.handleBinary(
            e.data,
            (frame) => sendWebSocketFrame(ws, frame),
            routeVerifiedSnapshot,
          )) return;
          throw new TypeError("unexpected binary WebSocket frame");
        }
        if (typeof e.data !== "string") {
          throw new TypeError("unexpected WebSocket frame type");
        }
        const parsed = parseWireEvent(JSON.parse(e.data));
        if (!parsed.ok) {
          throw new TypeError(`invalid WebSocket frame: ${parsed.error.code}`);
        }
        const event = parsed.event as WSEvent;
        eventType = event.type;
        if (event.type === "pong") {
          lastPongAtRef.current = Date.now();
          return;
        }
        if (!routingVerifiedSnapshot && snapshotTransportRef.current.handle(
          event,
          (frame) => sendWebSocketFrame(ws, frame),
          routeVerifiedSnapshot,
          byteSize,
        )) return;

        if (typeof event.seq === "number" && event.session_id) {
          optionsRef.current.onEventSeqAdvance?.(event.session_id, event.seq);
        }
        try {
          optionsRef.current.onBeforePublish?.(event);
        } catch {
          // Observers cannot interrupt authoritative event projection.
        }
        try {
          eventBus.publish(event.type, event.data);
        } catch {
          // Generic publication cannot interrupt authoritative projection.
        }
        verifiedRouteResult = optionsRef.current.onEvent?.(event);
        return verifiedRouteResult;
      } catch (err) {
        frameFailed = true;
        logFailure("websocket", "frame_failed", err, {
          bytes: byteSize,
          event_type: eventType,
        });
        // ignore parse errors
      } finally {
        if (!routingVerifiedSnapshot) {
          webSocketTrafficLog.recordInbound(
            eventType,
            byteSize,
            performance.now() - frameStartedAt,
            frameFailed,
          );
        }
        logTiming("websocket", "frame_dispatch", frameStartedAt, {
          bytes: byteSize,
          event_type: eventType,
        }, 100);
      }
    };

    wsRef.current = ws;
  }, []);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    const snapshotTransport = snapshotTransportRef.current;
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      if (heartbeatIntervalRef.current !== undefined) {
        clearInterval(heartbeatIntervalRef.current);
        heartbeatIntervalRef.current = undefined;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      if (!ws) return;
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.close();
      webSocketTrafficLog.flush("unmounted");
      snapshotTransport.clear();
    };
  }, [connect]);

  // Exposed so a resume/foreground event (Capacitor appStateChange, tab
  // visibility) can verify the socket immediately instead of waiting for
  // the next heartbeat cycle. A stalled-but-OPEN socket can't be told
  // apart from a healthy one synchronously, so this forces a close and
  // lets the existing onclose->reconnect path repair it; an already
  // closed/closing socket reconnects right away instead of waiting out
  // the fixed retry delay.
  const checkConnection = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
      return;
    }
    if (!ws || ws.readyState === WebSocket.CLOSED) {
      clearTimeout(reconnectTimer.current);
      connect();
    }
  }, [connect]);

  // Subscribe / unsubscribe lifecycle. When the user switches to a new
  // session (or the WS reconnects while one is open) we tell the
  // backend "I'm viewing this app_session_id now; push any
  // SessionWatcher events for it through my ws_callback." When they
  // switch away we unsubscribe the previous id so the backend stops
  // firing into a stale WS callback slot.
  //
  // Explicit subscriptions also cover viewing sessions without sending
  // through the socket, including recovered and externally-driven runs.
  // Track the full set of subscribed session ids (the focused pane plus
  // any additional pane ids from the split-fork view). Diff-driven: on
  // every change of the desired set, send subscribe frames for new ids
  // and unsubscribe for dropped ids.
  const subscribedIdsRef = useRef<Set<string>>(new Set());
  const targetAppSessionId = options.currentAppSessionId ?? null;
  const additionalIds = options.additionalAppSessionIds;
  // Memoize the joined key so this effect only re-runs when the set
  // actually changes (not on every parent render).
  const desiredSetKey = (() => {
    const ids = new Set<string>();
    if (targetAppSessionId) ids.add(targetAppSessionId);
    for (const id of additionalIds ?? []) {
      if (id) ids.add(id);
    }
    return Array.from(ids).sort().join("|");
  })();
  const subscribeSession = useCallback((id: string): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    if (subscribedIdsRef.current.has(id)) return true;
    sendWebSocketFrame(ws, {
      type: "subscribe",
      app_session_id: id,
      since_seq: optionsRef.current.getSinceSeq?.(id) ?? 0,
      events_from_seq: optionsRef.current.getEventsFromSeq?.(id) ?? 0,
      events_cursor_known: optionsRef.current.getEventsCursorKnown?.(id) ?? false,
    });
    subscribedIdsRef.current = new Set(subscribedIdsRef.current).add(id);
    return true;
  }, []);
  useEffect(() => {
    if (!connected || !subscriptionsReady) {
      // WS went down — drop our local record of subscriptions so that
      // the reconnect re-subscribes the full desired set fresh.
      subscribedIdsRef.current = new Set();
      return;
    }
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const desired = new Set<string>();
    if (targetAppSessionId) desired.add(targetAppSessionId);
    for (const id of additionalIds ?? []) {
      if (id) desired.add(id);
    }
    const prev = subscribedIdsRef.current;
    // Unsubscribe ids that fell out of the desired set.
    for (const id of prev) {
      if (!desired.has(id)) {
        try {
          sendWebSocketFrame(ws, { type: "unsubscribe", app_session_id: id });
          const next = new Set(subscribedIdsRef.current);
          next.delete(id);
          subscribedIdsRef.current = next;
        } catch {
          // ignore
        }
      }
    }
    // Subscribe ids that are newly desired.
    for (const id of desired) {
      try {
        subscribeSession(id);
      } catch {
        // ignore
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, desiredSetKey, subscriptionsReady]);

  const getReadyState = useCallback(
    () => wsRef.current?.readyState ?? WebSocket.CLOSED,
    [],
  );

  const send = useCallback((frame: WebSocketFrame): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    sendWebSocketFrame(ws, frame);
    return true;
  }, []);

  return {
    connected,
    checkConnection,
    getReadyState,
    send,
    subscribeSession,
  };
}
