import { act } from "@testing-library/react";
import type { WSEvent } from "../../src/types";

export interface OutboundFrame {
  type: string;
  [key: string]: unknown;
}

/** Replaces global WebSocket with a controllable shim. The single
 *  active instance is exposed via getCurrent() so tests can drive it. */
export class MockWebSocketController {
  private current: MockWebSocket | null = null;
  private sockets: MockWebSocket[] = [];
  private originalCtor: typeof WebSocket | undefined;
  private autoOpen: boolean;

  constructor(autoOpen = true) {
    this.autoOpen = autoOpen;
  }

  install(): void {
    this.originalCtor = globalThis.WebSocket;
    const getController = () => this;
    class Bound extends MockWebSocket {
      constructor(url: string, protocols?: string | string[]) {
        super(url, getController(), protocols);
      }
    }
    // Re-expose the static readyState constants so consumers can do
    // `WebSocket.OPEN`. Class-level properties via Object.defineProperty.
    Object.defineProperty(Bound, "CONNECTING", { value: 0 });
    Object.defineProperty(Bound, "OPEN", { value: 1 });
    Object.defineProperty(Bound, "CLOSING", { value: 2 });
    Object.defineProperty(Bound, "CLOSED", { value: 3 });
    globalThis.WebSocket = Bound as unknown as typeof WebSocket;
  }

  uninstall(): void {
    if (this.originalCtor) globalThis.WebSocket = this.originalCtor;
    this.originalCtor = undefined;
    this.current = null;
    this.sockets = [];
  }

  setCurrent(ws: MockWebSocket): void {
    this.current = ws;
    this.sockets.push(ws);
  }

  shouldAutoOpen(): boolean {
    return this.autoOpen;
  }

  getCurrent(): MockWebSocket {
    if (!this.current) throw new Error("MockWebSocket: no active instance");
    return this.current;
  }

  /** Most-recently-created OPEN socket whose URL contains `urlSubstring` —
   * for targeting a specific connection when more than one is live at
   * once (e.g. the legacy `/ws/chat` socket AND a native `/ws/v2/surface`
   * socket open simultaneously in the same test). `getCurrent()` above
   * only ever tracks "the last one created" and is unaware of URL, so it
   * is unsafe once a second concurrent socket exists — this is the
   * URL-disambiguated counterpart, additive, existing callers unaffected. */
  getCurrentByUrl(urlSubstring: string): MockWebSocket {
    const match = [...this.sockets]
      .reverse()
      .find((ws) => ws.url.includes(urlSubstring) && ws.readyState !== MockWebSocket.CLOSED);
    if (!match) {
      throw new Error(`MockWebSocket: no active instance matching "${urlSubstring}"`);
    }
    return match;
  }

  /** Push a WS frame into the app. Wrapped in act() so React state
   *  updates flush before the test asserts. */
  emit(event: WSEvent): void {
    const ws = this.getCurrent();
    act(() => {
      ws.deliver(event);
    });
  }

  emitMany(events: WSEvent[]): void {
    const ws = this.getCurrent();
    act(() => {
      for (const e of events) ws.deliver(e);
    });
  }

  /** Same as `emit`, targeting the socket whose URL contains
   *  `urlSubstring` instead of "whichever connected last". */
  emitTo(urlSubstring: string, frame: unknown): void {
    const ws = this.getCurrentByUrl(urlSubstring);
    act(() => {
      ws.deliverRaw(frame);
    });
  }

  /** All outbound .send() payloads, parsed. */
  get outbound(): OutboundFrame[] {
    return this.sockets.flatMap((ws) => ws.outbound);
  }

  /** Drop the current connection — exercises the reconnect path. */
  closeCurrent(): void {
    this.autoOpen = false;
    const ws = this.current;
    if (!ws) return;
    act(() => {
      ws.simulateClose();
    });
  }

  reopenCurrent(): void {
    this.autoOpen = true;
    const ws = this.getCurrent();
    act(() => {
      ws.simulateOpen();
    });
  }
}

export class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState: number = MockWebSocket.CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  url: string;
  protocols: string[];
  binaryType: BinaryType = "blob";
  outbound: OutboundFrame[] = [];

  constructor(
    url: string,
    controller: MockWebSocketController,
    protocols?: string | string[],
  ) {
    this.url = url;
    this.protocols = typeof protocols === "string" ? [protocols] : protocols ?? [];
    controller.setCurrent(this);
    // Open asynchronously so the consumer attaches handlers first.
    queueMicrotask(() => {
      if (!controller.shouldAutoOpen()) return;
      this.readyState = MockWebSocket.OPEN;
      this.onopen?.(new Event("open"));
    });
  }

  send(data: string): void {
    try {
      this.outbound.push(JSON.parse(data));
    } catch {
      this.outbound.push({ type: "raw", raw: data } as OutboundFrame);
    }
  }

  close(): void {
    this.simulateClose();
  }

  simulateClose(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  }

  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  deliver(event: WSEvent): void {
    this.deliverRaw(event);
  }

  /** Same as `deliver`, untyped — for wire shapes other than the legacy
   *  `/ws/chat` WSEvent union (e.g. Chat Surface Contract v2's ChatFrame
   *  over `/ws/v2/surface`, see adapter/wire.ts). Both are just
   *  JSON.stringify'd onto onmessage; only the TS type differs. */
  deliverRaw(data: unknown): void {
    if (this.readyState !== MockWebSocket.OPEN) return;
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }

  deliverBinary(data: ArrayBuffer): void {
    if (this.readyState !== MockWebSocket.OPEN) return;
    this.onmessage?.(new MessageEvent("message", { data }));
  }
}
