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
  private autoOpen = true;

  install(): void {
    this.originalCtor = globalThis.WebSocket;
    const getController = () => this;
    class Bound extends MockWebSocket {
      constructor(url: string) {
        super(url, getController());
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
  outbound: OutboundFrame[] = [];

  constructor(url: string, controller: MockWebSocketController) {
    this.url = url;
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
    if (this.readyState !== MockWebSocket.OPEN) return;
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }
}
