// @vitest-environment happy-dom

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  KNOWN_WEB_SOCKET_EVENT_TYPES,
  sendWebSocketFrame,
  utf8ByteLength,
  webSocketDataBytes,
  WebSocketTrafficLog,
} from "../src/lib/webSocketTrafficLog";

type Summary = {
  interval_ms: number;
  reason: string;
  summary_bytes: number;
  inbound: Record<string, Record<string, number>>;
  outbound: Record<string, Record<string, number>>;
};

function makeLog(nowRef: { value: number }) {
  const emit = vi.fn();
  return {
    emit,
    log: new WebSocketTrafficLog(emit, () => nowRef.value, 15_000),
  };
}

function lastSummary(emit: ReturnType<typeof vi.fn>): Summary {
  return emit.mock.calls.at(-1)?.[2] as Summary;
}

describe("WebSocket traffic log", () => {
  it("measures exact UTF-8 and binary payload sizes", () => {
    expect(utf8ByteLength("abc")).toBe(3);
    expect(utf8ByteLength("שלום 😀")).toBe(new TextEncoder().encode("שלום 😀").byteLength);
    expect(webSocketDataBytes("é")).toBe(2);
    expect(webSocketDataBytes(new Blob(["abcd"]))).toBe(4);
    expect(webSocketDataBytes(new Uint8Array(7))).toBe(7);
    expect(webSocketDataBytes(new ArrayBuffer(9))).toBe(9);
  });

  it("emits bounded periodic summaries by direction and type", () => {
    const now = { value: 0 };
    const { emit, log } = makeLog(now);

    log.recordInbound("messages_delta", 101, 12.345, false);
    log.recordOutbound("subscribe", 41, 900, false);
    now.value = 15_000;
    log.recordInbound("messages_delta", 99, 7, true);

    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith("websocket-traffic", "summary", expect.any(Object));
    const summary = lastSummary(emit);
    expect(summary.interval_ms).toBe(15_000);
    expect(summary.reason).toBe("periodic");
    expect(summary.inbound.messages_delta).toMatchObject({
      frames: 2,
      bytes: 200,
      failures: 1,
      processing_ms_total: 19.35,
      processing_ms_max: 12.35,
    });
    expect(summary.outbound.subscribe).toMatchObject({
      frames: 1,
      bytes: 41,
      failures: 0,
      buffered_bytes_max: 900,
    });
    expect(summary.summary_bytes).toBeLessThan(16_384);
  });

  it("bounds untrusted type cardinality and never includes payload content", () => {
    const now = { value: 0 };
    const { emit, log } = makeLog(now);
    const secret = "PROMPT_SECRET_SHOULD_NOT_APPEAR";

    for (const type of [...KNOWN_WEB_SOCKET_EVENT_TYPES].slice(0, 80)) {
      log.recordInbound(type, secret.length, 1, false);
    }
    log.recordInbound("identifier_SECRET_SENTINEL", secret.length, 1, false);
    log.flush("unmounted");

    const summary = lastSummary(emit);
    expect(Object.keys(summary.inbound).length).toBeLessThanOrEqual(65);
    expect(summary.inbound.other.frames).toBe(17);
    expect(JSON.stringify(summary)).not.toContain(secret);
    expect(summary.summary_bytes).toBeLessThanOrEqual(12_000);
  });

  it("records send bytes, buffered pressure, and send failures", () => {
    const now = { value: 0 };
    const { emit, log } = makeLog(now);
    const sent: string[] = [];
    const socket = {
      bufferedAmount: 0,
      send(text: string) {
        sent.push(text);
        this.bufferedAmount = 321;
      },
    };

    sendWebSocketFrame(socket, { type: "subscribe", value: "é" }, log);
    expect(sent).toHaveLength(1);

    const failingSocket = {
      bufferedAmount: 444,
      send: vi.fn(() => { throw new Error("closed"); }),
    };
    expect(() => sendWebSocketFrame(failingSocket, { type: "stop_message" }, log)).toThrow("closed");
    log.flush("connection_closed");

    const summary = lastSummary(emit);
    expect(summary.outbound.subscribe.bytes).toBe(utf8ByteLength(sent[0]));
    expect(summary.outbound.subscribe.buffered_bytes_max).toBe(321);
    expect(summary.outbound.stop_message.failures).toBe(1);
  });

  it("resets after flush and does not emit empty summaries", () => {
    const now = { value: 0 };
    const { emit, log } = makeLog(now);

    log.recordInbound("turn_start", 10, 1, false);
    log.flush("connection_closed");
    log.flush("unmounted");
    expect(emit).toHaveBeenCalledTimes(1);

    now.value = 20;
    log.recordInbound("turn_complete", 20, 2, false);
    log.flush("unmounted");
    expect(lastSummary(emit).inbound).toEqual({
      turn_complete: expect.objectContaining({ frames: 1, bytes: 20 }),
    });
  });

  it("never lets telemetry emission alter the WebSocket path", () => {
    const log = new WebSocketTrafficLog(
      () => { throw new Error("logger unavailable"); },
      () => 20_000,
      15_000,
    );
    const socket = { bufferedAmount: 0, send: vi.fn() };

    expect(() => sendWebSocketFrame(socket, { type: "subscribe" }, log)).not.toThrow();
    expect(socket.send).toHaveBeenCalledTimes(1);
  });

  it("keeps every useWebSocket send on the measured wrapper", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/hooks/useWebSocket.ts"),
      "utf8",
    );
    expect(source).not.toMatch(/\b(?:ws|wsRef\.current)\.send\s*\(/);
    expect(source).toContain("sendWebSocketFrame");
  });

  it("allowlists every declared frontend WebSocket event type", () => {
    const source = readFileSync(resolve(process.cwd(), "src/types.ts"), "utf8");
    const eventTypeBlock = source.slice(
      source.indexOf("export type WSEventType"),
      source.indexOf("export interface WSEvent"),
    );
    const declared = [...eventTypeBlock.matchAll(/^\s*\| "([^"]+)"/gm)].map((match) => match[1]);

    expect(declared.length).toBeGreaterThan(80);
    expect(declared.filter((type) => !KNOWN_WEB_SOCKET_EVENT_TYPES.has(type))).toEqual([]);
  });
});
