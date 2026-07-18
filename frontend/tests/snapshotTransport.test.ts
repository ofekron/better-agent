import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SnapshotTransport } from "../src/lib/snapshotTransport";
import { useWebSocket } from "../src/hooks/useWebSocket";
import type { WSEvent } from "../src/types";
import { MockWebSocketController } from "./harness/mockWebSocket";

const encoder = new TextEncoder();
const REFRESH_ID = "f".repeat(32);

async function digest(bytes: Uint8Array): Promise<string> {
  const value = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(value), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function frames(event: WSEvent, chunkBytes = 9) {
  const bytes = encoder.encode(JSON.stringify(event));
  const payloadDigest = await digest(bytes);
  const revision = `sha256:${payloadDigest}`;
  const totalChunks = Math.ceil(bytes.length / chunkBytes);
  return {
    begin: { type: "snapshot_begin", data: {
      snapshot_id: "snap-1", key: "messages_replay:s1", event_type: event.type,
      refresh_id: REFRESH_ID,
      revision, digest: payloadDigest, total_bytes: bytes.length,
      total_chunks: totalChunks, chunk_bytes: chunkBytes, resume_from: 0,
    } },
    chunks: Array.from({ length: totalChunks }, (_, index) => ({
      type: "snapshot_chunk",
      data: { snapshot_id: "snap-1", revision, index,
        payload: base64(bytes.slice(index * chunkBytes, (index + 1) * chunkBytes)) },
    })),
    end: { type: "snapshot_end", data: {
      snapshot_id: "snap-1", revision, digest: payloadDigest,
      total_bytes: bytes.length, total_chunks: totalChunks,
    } },
    revision,
  };
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 5));
  await new Promise((resolve) => setTimeout(resolve, 5));
}

describe("SnapshotTransport", () => {
  it("accepts reordered and identical duplicate chunks, then applies atomically", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const payload = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });

    transport.handle(payload.begin, send, apply);
    for (const chunk of [...payload.chunks].reverse()) transport.handle(chunk, send, apply);
    transport.handle(payload.chunks[0], send, apply);
    transport.handle(payload.end, send, apply);

    expect(apply).not.toHaveBeenCalled();
    await settle();
    expect(apply).toHaveBeenCalledOnce();
    expect(apply).toHaveBeenCalledWith({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "snapshot_ack", data: { next_chunk: payload.chunks.length },
    });
  });

  it("emits exactly one cumulative ACK for each accepted chunk", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const payload = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [],
    } });
    transport.handle(payload.begin, send, vi.fn());
    send.mockClear();
    transport.handle(payload.chunks[0], send, vi.fn());
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "snapshot_ack",
      data: {
        snapshot_id: "snap-1", revision: payload.revision, next_chunk: 1,
      },
    });
  });

  it("decodes a near-limit chunk cooperatively before acknowledging it", async () => {
    vi.useFakeTimers();
    try {
      const transport = new SnapshotTransport();
      const send = vi.fn();
      const payload = await frames({ type: "messages_replay", data: {
        app_session_id: "s1", messages: [{ id: "large", text: "x".repeat(170 * 1024) }],
      } } as WSEvent, 180 * 1024);
      transport.handle(payload.begin, send, vi.fn());
      send.mockClear();

      transport.handle(payload.chunks[0], send, vi.fn());

      expect(send).not.toHaveBeenCalled();
      await Promise.resolve();
      await vi.advanceTimersToNextTimerAsync();
      expect(send).not.toHaveBeenCalled();
      await vi.runAllTimersAsync();
      expect(send).toHaveBeenCalledOnce();
      expect(send).toHaveBeenCalledWith({
        type: "snapshot_ack",
        data: {
          snapshot_id: "snap-1", revision: payload.revision, next_chunk: 1,
        },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not acknowledge or mutate a transfer cancelled during cooperative decoding", async () => {
    vi.useFakeTimers();
    try {
      const transport = new SnapshotTransport();
      const send = vi.fn();
      const apply = vi.fn();
      const payload = await frames({ type: "messages_replay", data: {
        app_session_id: "s1", messages: [{ id: "large", text: "x".repeat(170 * 1024) }],
      } } as WSEvent, 180 * 1024);
      transport.handle(payload.begin, send, apply);
      send.mockClear();
      transport.handle(payload.chunks[0], send, apply);
      await Promise.resolve();
      transport.handle({ type: "snapshot_cancelled", data: {
        snapshot_id: "snap-1", revision: payload.revision, reason: "superseded",
      } }, send, apply);

      await vi.runAllTimersAsync();
      expect(send).not.toHaveBeenCalled();
      expect(apply).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("recovers from malformed base64 without acknowledging the corrupt chunk", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const payload = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [],
    } });
    transport.handle(payload.begin, send, apply);
    send.mockClear();
    const malformed = structuredClone(payload.chunks[0]);
    malformed.data.payload = "%%%=";
    transport.handle(malformed, send, apply);
    await settle();

    expect(send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "snapshot_ack" }));
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "snapshot_refresh", data: { reason: "corrupt" },
    });
    expect(apply).not.toHaveBeenCalled();
  });

  it("bounds queued chunk work and reclaims capacity after overflow recovery", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const payload = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [],
    } }, 180 * 1024);
    transport.handle(payload.begin, send, apply);
    send.mockClear();
    for (let index = 0; index <= 128; index += 1) {
      transport.handle(payload.chunks[0], send, apply);
    }
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "snapshot_refresh", data: { reason: "overflow" },
    });
    await settle();

    const next = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [{ id: "next" }],
    } }, 180 * 1024);
    send.mockClear();
    transport.handle(next.begin, send, apply);
    send.mockClear();
    transport.handle(next.chunks[0], send, apply);
    await settle();
    expect(send).toHaveBeenCalledWith({
      type: "snapshot_ack",
      data: { snapshot_id: "snap-1", revision: next.revision, next_chunk: 1 },
    });
  });

  it("never applies missing, digest-corrupt, or conflicting duplicate data", async () => {
    const source = { type: "messages_replay", data: { app_session_id: "s1", messages: [] } } as WSEvent;
    for (const mode of ["missing", "digest", "duplicate"] as const) {
      const transport = new SnapshotTransport();
      const apply = vi.fn();
      const payload = await frames(source);
      transport.handle(payload.begin, vi.fn(), apply);
      const chunks = mode === "missing" ? payload.chunks.slice(1) : payload.chunks;
      for (const chunk of chunks) transport.handle(chunk, vi.fn(), apply);
      if (mode === "digest") {
        const last = payload.chunks.at(-1)!;
        const changed = structuredClone(last);
        changed.data.payload = base64(Uint8Array.from(atob(last.data.payload), (char, index) =>
          index === 0 ? char.charCodeAt(0) ^ 1 : char.charCodeAt(0)));
        transport.handle(changed, vi.fn(), apply);
      }
      if (mode === "duplicate") {
        const changed = structuredClone(payload.chunks[0]);
        changed.data.payload = base64(Uint8Array.of(...new Uint8Array(atob(changed.data.payload).split("").map(c => c.charCodeAt(0))).map((b, i) => i ? b : b ^ 1)));
        transport.handle(changed, vi.fn(), apply);
      }
      transport.handle(payload.end, vi.fn(), apply);
      await settle();
      expect(apply, mode).not.toHaveBeenCalled();
    }
  });

  it("replaces a key on revision change and discards a cache miss", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const first = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });
    const second = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [{ id: "new" }] } } as WSEvent);
    second.begin.data.snapshot_id = "snap-2";
    second.chunks.forEach((chunk) => { chunk.data.snapshot_id = "snap-2"; });
    second.end.data.snapshot_id = "snap-2";

    transport.handle(first.begin, send, apply);
    transport.handle(first.chunks[0], send, apply);
    transport.handle(second.begin, send, apply);
    transport.resume(send);
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({ type: "snapshot_resume", data: { snapshot_id: "snap-2", next_chunk: 0 } });
    transport.handle({ type: "snapshot_restart_required", data: {
      snapshot_id: "snap-2", revision: second.revision, reason: "expired",
    } }, send, apply);
    transport.resume(send);
    expect(send.mock.calls.filter(([frame]) => frame.type === "snapshot_resume")).toHaveLength(1);
    expect(apply).not.toHaveBeenCalled();
  });

  it("drops frames between supersession cancellation and its replacement begin", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const old = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [{ id: "old" }],
    } });
    transport.handle(old.begin, send, apply);
    transport.handle({ type: "messages_delta", data: {
      app_session_id: "s1", messages: [{ id: "before-cancel" }],
    } }, send, apply, 10);
    transport.handle({ type: "snapshot_cancelled", data: {
      snapshot_id: "snap-1", revision: old.revision, reason: "superseded",
    } }, send, apply);
    transport.handle({ type: "messages_delta", data: {
      app_session_id: "s1", messages: [{ id: "between" }],
    } }, send, apply, 10);
    const replacement = await frames({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [{ id: "replacement" }],
    } });
    replacement.begin.data.snapshot_id = "snap-2";
    replacement.chunks.forEach((chunk) => { chunk.data.snapshot_id = "snap-2"; });
    replacement.end.data.snapshot_id = "snap-2";
    transport.handle(replacement.begin, send, apply);
    replacement.chunks.forEach((chunk) => transport.handle(chunk, send, apply));
    transport.handle(replacement.end, send, apply);
    await settle();
    expect(apply).toHaveBeenCalledOnce();
    expect(apply).toHaveBeenCalledWith({ type: "messages_replay", data: {
      app_session_id: "s1", messages: [{ id: "replacement" }],
    } });
  });

  it("resumes from the cumulative contiguous offset without losing staged chunks", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const payload = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });
    transport.handle(payload.begin, send, apply);
    transport.handle(payload.chunks[0], send, apply);
    await settle();
    transport.resume(send);
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "snapshot_resume", data: { snapshot_id: "snap-1", next_chunk: 1 },
    });
    const resumedBegin = structuredClone(payload.begin);
    resumedBegin.data.resume_from = 1;
    transport.handle(resumedBegin, send, apply);
    payload.chunks.slice(1).forEach((chunk) => transport.handle(chunk, send, apply));
    transport.handle(payload.end, send, apply);
    await settle();
    expect(apply).toHaveBeenCalledOnce();
  });

  it("yields before verification and parsing so receipt does not synchronously block UI work", async () => {
    const transport = new SnapshotTransport();
    const apply = vi.fn();
    const payload = await frames({
      type: "rewind_complete", data: { session_id: "s1", messages: [] },
    });
    transport.handle(payload.begin, vi.fn(), apply);
    payload.chunks.forEach((chunk) => transport.handle(chunk, vi.fn(), apply));
    transport.handle(payload.end, vi.fn(), apply);
    const uiWork = vi.fn();
    queueMicrotask(uiWork);
    await Promise.resolve();
    expect(uiWork).toHaveBeenCalledOnce();
    expect(apply).not.toHaveBeenCalled();
    await settle();
    expect(apply).toHaveBeenCalledOnce();
  });

  it("holds interleaved live frames and drains them after the snapshot in wire order", async () => {
    const transport = new SnapshotTransport();
    const applied: WSEvent[] = [];
    const apply = (event: WSEvent) => applied.push(event);
    const payload = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });
    const live = { type: "messages_delta", data: { app_session_id: "s1", messages: [{ id: "new" }] } } as WSEvent;
    transport.handle(payload.begin, vi.fn(), apply);
    transport.handle(live, vi.fn(), apply, 100);
    payload.chunks.forEach((chunk) => transport.handle(chunk, vi.fn(), apply));
    transport.handle(payload.end, vi.fn(), apply);
    expect(applied).toEqual([]);
    await settle();
    expect(applied).toEqual([
      { type: "messages_replay", data: { app_session_id: "s1", messages: [] } },
      live,
    ]);
  });

  it.each(["cache-miss", "corrupt", "overflow"] as const)(
    "%s drops dependent deltas and converges only after a fresh authority snapshot",
    async (failure) => {
      const transport = new SnapshotTransport();
      const send = vi.fn();
      const applied: WSEvent[] = [];
      let resolveRest!: () => void;
      const restComplete = new Promise<void>((resolve) => { resolveRest = resolve; });
      const apply = vi.fn((event: WSEvent) => {
        if (event.type === "session_reconciled") return restComplete;
        applied.push(event);
      });
      const old = await frames({ type: "messages_replay", data: {
        app_session_id: "s1", messages: [{ id: "old" }],
      } });
      transport.handle(old.begin, send, apply);
      transport.handle({ type: "messages_delta", data: {
        app_session_id: "s1", messages: [{ id: "dependent" }],
      } }, send, apply, 10);

      if (failure === "cache-miss") {
        transport.handle({ type: "snapshot_restart_required", data: {
          snapshot_id: "snap-1", revision: old.revision, reason: "not_found",
        } }, send, apply);
      } else if (failure === "corrupt") {
        transport.handle(old.chunks[0], send, apply);
        const conflicting = structuredClone(old.chunks[0]);
        const bytes = Uint8Array.from(atob(conflicting.data.payload), (char) => char.charCodeAt(0));
        bytes[0] ^= 1;
        conflicting.data.payload = base64(bytes);
        transport.handle(conflicting, send, apply);
      } else {
        for (let index = 0; index <= 2048; index += 1) {
          transport.handle({ type: "messages_delta", data: {
            app_session_id: "s1", messages: [{ id: `buffered-${index}` }],
          } }, send, apply, 1);
        }
      }

      await settle();

      expect(applied).toEqual([]);
      expect(send.mock.calls.some(([frame]) => frame.type === "snapshot_refresh")).toBe(true);

      const beforeAuthority = { type: "messages_delta", data: {
        app_session_id: "s1", messages: [{ id: "before-authority" }],
      } } as WSEvent;
      transport.handle(beforeAuthority, send, apply, 10);
      transport.handle({ type: "session_reconciled", data: {
        root_id: "s1", scope_sids: ["s1"], snapshot_refresh_id: REFRESH_ID,
      } }, send, apply, 10);
      const afterAuthority = { type: "messages_delta", data: {
        app_session_id: "s1", messages: [{ id: "after-authority" }],
      } } as WSEvent;
      transport.handle(afterAuthority, send, apply, 10);
      transport.handle({ type: "snapshot_refresh_complete", data: {
        refresh_id: REFRESH_ID, success: true, root_ids: ["s1"],
      } }, send, apply);
      expect(applied).toEqual([]);
      resolveRest();
      await settle();
      expect(applied).toEqual([afterAuthority]);
      expect(apply).not.toHaveBeenCalledWith(beforeAuthority);
    },
  );

  it("rejects payloads above 16 MiB into bounded authoritative refresh recovery", async () => {
    const transport = new SnapshotTransport();
    const send = vi.fn();
    const apply = vi.fn();
    const revision = `sha256:${"a".repeat(64)}`;
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "messages_replay:s1", event_type: "messages_replay", revision,
      refresh_id: REFRESH_ID,
      reason: "too_large",
    } }, send, apply);
    expect(send).toHaveBeenLastCalledWith({
      type: "snapshot_refresh",
      data: {
        key: "messages_replay:s1", event_type: "messages_replay",
        failed_revision: revision, refresh_id: REFRESH_ID, reason: "too_large",
      },
    });
    expect(apply).not.toHaveBeenCalled();

    send.mockClear();
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "messages_replay:s1", event_type: "messages_replay", revision,
      refresh_id: REFRESH_ID, reason: "overflow",
    } }, send, apply);
    expect(send.mock.calls.at(-1)?.[0]).toMatchObject({
      type: "snapshot_refresh", data: { reason: "overflow" },
    });
  });

  it("preserves A-live beyond A authority when the later B authority marker arrives", async () => {
    const transport = new SnapshotTransport();
    const applied: WSEvent[] = [];
    const apply = vi.fn((event: WSEvent) => {
      if (event.type !== "session_reconciled") applied.push(event);
      return Promise.resolve();
    });
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "stub_invalidated:global", event_type: "stub_invalidated",
      revision: `sha256:${"a".repeat(64)}`, refresh_id: REFRESH_ID,
      reason: "overflow",
    } }, vi.fn(), apply);
    const staleFork = { type: "messages_delta", data: { app_session_id: "F", messages: [{ id: "stale" }] } } as WSEvent;
    transport.handle(staleFork, vi.fn(), apply, 10);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "A", scope_sids: ["A", "F"], snapshot_refresh_id: REFRESH_ID,
    } }, vi.fn(), apply);
    const aLive = { type: "messages_delta", data: { app_session_id: "A", messages: [] } } as WSEvent;
    const forkLive = { type: "messages_delta", data: { app_session_id: "F", messages: [] } } as WSEvent;
    const staleB = { type: "messages_delta", data: { app_session_id: "B", messages: [] } } as WSEvent;
    transport.handle(aLive, vi.fn(), apply, 10);
    transport.handle(forkLive, vi.fn(), apply, 10);
    transport.handle(staleB, vi.fn(), apply, 10);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "B", scope_sids: ["B"], snapshot_refresh_id: REFRESH_ID,
    } }, vi.fn(), apply);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: REFRESH_ID, success: true, root_ids: ["A", "B"],
    } }, vi.fn(), apply);
    await settle();
    expect(applied).toEqual([aLive, forkLive]);
    expect(applied).not.toContain(staleB);
    expect(applied).not.toContain(staleFork);
  });

  it("isolates concurrent refresh IDs until both authority boundaries complete", async () => {
    const transport = new SnapshotTransport();
    const refreshA = "a".repeat(32);
    const refreshB = "b".repeat(32);
    const applied: WSEvent[] = [];
    const apply = vi.fn((event: WSEvent) => {
      if (event.type !== "session_reconciled") applied.push(event);
      return Promise.resolve();
    });
    const beginRecovery = (rootId: string, refreshId: string) => transport.handle({
      type: "snapshot_refresh_required",
      data: {
        key: `messages_replay:${rootId}`, event_type: "messages_replay",
        revision: `sha256:${rootId.toLowerCase().repeat(64)}`,
        refresh_id: refreshId, reason: "too_large",
      },
    }, vi.fn(), apply);
    beginRecovery("A", refreshA);
    beginRecovery("B", refreshB);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "A", scope_sids: ["A", "FA"], snapshot_refresh_id: refreshA,
    } }, vi.fn(), apply);
    const aLive = { type: "messages_delta", data: { app_session_id: "A", messages: [] } } as WSEvent;
    transport.handle(aLive, vi.fn(), apply, 10);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "B", scope_sids: ["B"], snapshot_refresh_id: refreshB,
    } }, vi.fn(), apply);
    const bLive = { type: "messages_delta", data: { app_session_id: "B", messages: [] } } as WSEvent;
    transport.handle(bLive, vi.fn(), apply, 10);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: refreshA, success: true, root_ids: ["A"],
    } }, vi.fn(), apply);
    await settle();
    expect(applied).toEqual([]);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: refreshB, success: true, root_ids: ["B"],
    } }, vi.fn(), apply);
    await settle();
    expect(applied).toEqual([aLive, bLive]);
  });

  it.each([
    ["missing root", ["F"]],
    ["duplicate", ["A", "A"]],
    ["unsorted", ["F", "A"]],
    ["too many", ["A", ...Array.from({ length: 512 }, (_, index) => `S${String(index).padStart(3, "0")}`)]],
    ["SID too long", ["A", "F".repeat(257)]],
  ] as const)("rejects malformed authority scope: %s", async (_name, scopeSids) => {
    const transport = new SnapshotTransport();
    const apply = vi.fn(() => Promise.resolve());
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "messages_replay:A", event_type: "messages_replay",
      revision: `sha256:${"a".repeat(64)}`, refresh_id: REFRESH_ID,
      reason: "too_large",
    } }, vi.fn(), apply);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "A", scope_sids: scopeSids, snapshot_refresh_id: REFRESH_ID,
    } }, vi.fn(), apply);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: REFRESH_ID, success: true, root_ids: ["A"],
    } }, vi.fn(), apply);
    await settle();
    expect(apply).not.toHaveBeenCalled();
  });

  it("rejects authority scopes above the aggregate UTF-8 bound", async () => {
    const transport = new SnapshotTransport();
    const apply = vi.fn(() => Promise.resolve());
    const prefix = "😀".repeat(255);
    const scopeSids = [
      "A",
      ...Array.from({ length: 129 }, (_, index) => `${prefix}${String.fromCodePoint(0x1000 + index)}`),
    ];
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "messages_replay:A", event_type: "messages_replay",
      revision: `sha256:${"a".repeat(64)}`, refresh_id: REFRESH_ID,
      reason: "too_large",
    } }, vi.fn(), apply);
    transport.handle({ type: "session_reconciled", data: {
      root_id: "A", scope_sids: scopeSids, snapshot_refresh_id: REFRESH_ID,
    } }, vi.fn(), apply);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: REFRESH_ID, success: true, root_ids: ["A"],
    } }, vi.fn(), apply);
    await settle();
    expect(apply).not.toHaveBeenCalled();
  });

  it("keeps recovery blocked when the backend terminal fails closed", async () => {
    const transport = new SnapshotTransport();
    const apply = vi.fn(() => Promise.resolve());
    transport.handle({ type: "snapshot_refresh_required", data: {
      key: "messages_replay:A", event_type: "messages_replay",
      revision: `sha256:${"a".repeat(64)}`, refresh_id: REFRESH_ID,
      reason: "too_large",
    } }, vi.fn(), apply);
    transport.handle({ type: "messages_delta", data: {
      app_session_id: "A", messages: [],
    } }, vi.fn(), apply, 10);
    transport.handle({ type: "snapshot_refresh_complete", data: {
      refresh_id: REFRESH_ID, success: false, root_ids: [],
    } }, vi.fn(), apply);
    await settle();
    expect(apply).not.toHaveBeenCalled();
  });
});

describe("useWebSocket snapshot routing", () => {
  it("keeps live reducers gated until correlated authoritative REST reconciliation resolves", async () => {
    const ctrl = new MockWebSocketController();
    ctrl.install();
    let resolveRest!: () => void;
    const restComplete = new Promise<void>((resolve) => { resolveRest = resolve; });
    const onSessionReconciled = vi.fn(() => restComplete);
    const onMessagesDelta = vi.fn();
    const rendered = renderHook(() => useWebSocket("ws://test", {
      onSessionReconciled, onMessagesDelta,
    }));
    await act(async () => { await Promise.resolve(); });
    const revision = `sha256:${"a".repeat(64)}`;
    act(() => {
      ctrl.getCurrent().deliver({ type: "snapshot_refresh_required", data: {
        key: "messages_replay:s1", event_type: "messages_replay", revision,
        refresh_id: REFRESH_ID, reason: "too_large",
      } });
      ctrl.getCurrent().deliver({ type: "messages_delta", data: {
        app_session_id: "s1", messages: [{ id: "discarded" }],
      } });
      ctrl.getCurrent().deliver({ type: "session_reconciled", data: {
        root_id: "s1", scope_sids: ["s1"], snapshot_refresh_id: REFRESH_ID,
      } });
      ctrl.getCurrent().deliver({ type: "messages_delta", data: {
        app_session_id: "s1", messages: [{ id: "after-authority" }],
      } });
      ctrl.getCurrent().deliver({ type: "snapshot_refresh_complete", data: {
        refresh_id: REFRESH_ID, success: true, root_ids: ["s1"],
      } });
    });
    expect(onSessionReconciled).toHaveBeenCalledWith("s1", true);
    expect(onMessagesDelta).not.toHaveBeenCalled();
    await act(async () => { resolveRest(); await settle(); });
    expect(onMessagesDelta).toHaveBeenCalledOnce();
    expect(onMessagesDelta.mock.calls[0][1]).toEqual([{ id: "after-authority" }]);
    rendered.unmount();
    ctrl.uninstall();
  });

  it("routes verified replay, stub, and rewind frames through their existing callbacks", async () => {
    const ctrl = new MockWebSocketController();
    ctrl.install();
    const onMessagesReplay = vi.fn();
    const onMessagesDelta = vi.fn();
    const onStubInvalidated = vi.fn();
    const onRewindComplete = vi.fn();
    const rendered = renderHook(() => useWebSocket("ws://test", {
      onMessagesReplay, onMessagesDelta, onStubInvalidated, onRewindComplete,
    }));
    await act(async () => { await Promise.resolve(); });
    const payload = await frames({ type: "messages_replay", data: { app_session_id: "s1", messages: [] } });
    act(() => {
      ctrl.getCurrent().deliver(payload.begin as WSEvent);
      ctrl.getCurrent().deliver({
        type: "messages_delta", data: { app_session_id: "s1", messages: [{ id: "live" }] },
      });
      payload.chunks.forEach((chunk) => ctrl.getCurrent().deliver(chunk as WSEvent));
      ctrl.getCurrent().deliver(payload.end as WSEvent);
    });
    expect(onMessagesReplay).not.toHaveBeenCalled();
    await act(async () => { await settle(); });
    expect(onMessagesReplay).toHaveBeenCalledOnce();
    expect(onMessagesReplay).toHaveBeenCalledWith("s1", []);
    expect(onMessagesDelta).toHaveBeenCalledOnce();
    expect(onMessagesReplay.mock.invocationCallOrder[0]).toBeLessThan(
      onMessagesDelta.mock.invocationCallOrder[0],
    );

    const stub = { event_count: 1, last_events: [] };
    const stubPayload = await frames({ type: "stub_invalidated", data: {
      app_session_id: "s1", msg_id: "m1", stub,
    } });
    stubPayload.begin.data.snapshot_id = "snap-stub";
    stubPayload.chunks.forEach((chunk) => { chunk.data.snapshot_id = "snap-stub"; });
    stubPayload.end.data.snapshot_id = "snap-stub";
    act(() => {
      ctrl.getCurrent().deliver(stubPayload.begin as WSEvent);
      stubPayload.chunks.forEach((chunk) => ctrl.getCurrent().deliver(chunk as WSEvent));
      ctrl.getCurrent().deliver(stubPayload.end as WSEvent);
    });
    await act(async () => { await settle(); });
    expect(onStubInvalidated).toHaveBeenCalledWith("s1", "m1", stub);

    const rewindPayload = await frames({
      type: "rewind_complete", data: { session_id: "s1", messages: [] },
    });
    rewindPayload.begin.data.snapshot_id = "snap-rewind";
    rewindPayload.chunks.forEach((chunk) => { chunk.data.snapshot_id = "snap-rewind"; });
    rewindPayload.end.data.snapshot_id = "snap-rewind";
    act(() => {
      ctrl.getCurrent().deliver(rewindPayload.begin as WSEvent);
      rewindPayload.chunks.forEach((chunk) => ctrl.getCurrent().deliver(chunk as WSEvent));
      ctrl.getCurrent().deliver(rewindPayload.end as WSEvent);
    });
    await act(async () => { await settle(); });
    expect(onRewindComplete).toHaveBeenCalledWith("s1", []);
    rendered.unmount();
    ctrl.uninstall();
  });
});
