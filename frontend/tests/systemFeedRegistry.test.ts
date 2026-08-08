// Coverage for `src/lib/surfaceFeedSocket.ts` (the generic ref-counted
// feed-socket factory, ADR 0011 Package C's "one shared implementation"
// requirement) and its first consumer, `src/lib/systemFeedRegistry.ts`
// (ADR 0011's 12 system feeds). Both are true module-level singletons, so
// every test re-imports fresh via `vi.resetModules()` — same isolation
// pattern `tests/useProviderSurfaceSocket.test.ts` already uses for the
// provider plane's equivalent module.

import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocketController } from "./harness/mockWebSocket";
import type { HarnessProfileUpsertFrame, SystemFrame } from "../src/adapter/wire";

const isHarnessProfileUpsert = (frame: SystemFrame): frame is HarnessProfileUpsertFrame =>
  frame.type === "harness_profile_upsert";

const matchesHarnessProfileUpsertIntent = (
  frame: SystemFrame, intentId: string,
): frame is HarnessProfileUpsertFrame => isHarnessProfileUpsert(frame) && frame.intent_id === intentId;

// `tests/setup.ts` globally stubs `lib/systemFeedRegistry` (same rationale
// as `sessionSurfaceRegistry`/`interactionResolveSocket`) so the broad
// suite never opens a real WS from a directly-mounted component test —
// this file explicitly wants the REAL implementation, same escape hatch
// `tests/sessionSurfaceRegistry.test.ts`/`tests/interactionResolveSocket
// .test.tsx` already use.
vi.unmock("../src/lib/systemFeedRegistry");

let ws: MockWebSocketController;

beforeEach(() => {
  ws = new MockWebSocketController();
  ws.install();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  ws.uninstall();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("subscribeSystemFrames", () => {
  it("opens the shared connection and subscribes to all 12 system feeds", async () => {
    vi.resetModules();
    const { subscribeSystemFrames, SYSTEM_FEED_NAMES } = await import(
      "../src/lib/systemFeedRegistry"
    );
    expect(SYSTEM_FEED_NAMES).toHaveLength(12);
    const received: unknown[] = [];
    const unsubscribe = subscribeSystemFrames((f) => received.push(f));

    await vi.waitFor(() => {
      const sent = ws.outbound.find((f) => Array.isArray((f as { feeds?: unknown[] }).feeds));
      expect(sent).toBeTruthy();
      for (const name of SYSTEM_FEED_NAMES) {
        expect((sent as { feeds: unknown[] }).feeds).toContain(name);
      }
    });

    act(() => {
      ws.emit({ type: "installation_capability_changed", cv: 1, capability: { capability_id: "mobile", cv: 1, enabled: true, display: "Mobile" } } as never);
    });
    expect(received).toHaveLength(1);

    unsubscribe();
  });

  it("closes the connection once the last subscriber unsubscribes", async () => {
    vi.resetModules();
    const { subscribeSystemFrames, isSystemSocketOpen } = await import(
      "../src/lib/systemFeedRegistry"
    );
    const unsubscribe = subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(isSystemSocketOpen()).toBe(true));
    unsubscribe();
    expect(isSystemSocketOpen()).toBe(false);
  });

  it("routes a machine-topology `machine_node_upsert` frame here, never to a chat ChatFrame handler", async () => {
    vi.resetModules();
    const { subscribeSystemFrames } = await import("../src/lib/systemFeedRegistry");
    const received: unknown[] = [];
    const unsubscribe = subscribeSystemFrames((f) => received.push(f));
    await vi.waitFor(() => expect(ws.outbound.some((f) => Array.isArray((f as { feeds?: unknown[] }).feeds))).toBe(true));

    act(() => {
      ws.emit({
        type: "machine_node_upsert",
        cv: 1,
        node: { node_id: "n1", cv: 1, role: "worker_node", address: "10.0.0.2", cwd_roots: [], state: "connected", last_seen: null, connected_at: null, version_status: "ok" },
      } as never);
    });

    expect(received).toHaveLength(1);
    expect((received[0] as { type: string }).type).toBe("machine_node_upsert");
    unsubscribe();
  });
});

describe("submitSystemIntent", () => {
  it("returns null (no queueing) when the connection is not open", async () => {
    vi.resetModules();
    const { submitSystemIntent } = await import("../src/lib/systemFeedRegistry");
    expect(submitSystemIntent({ kind: "delete_schedule", schedule_id: "s1" })).toBeNull();
  });

  it("stamps cv/intent_id/session_id and sends over the shared connection once open", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    mod.submitSystemIntent({ kind: "remove_node", node_id: "n1" });

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "remove_node",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent.node_id).toBe("n1");
    expect(sent!.intent.session_id).toBeNull();
    expect(typeof sent!.intent.intent_id).toBe("string");
    expect(sent!.intent.intent_id).not.toBe("");

    unsubscribe();
  });

  it("resolves as intent_rejected immediately on a synchronous rejection", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const ackPromise = mod.submitSystemIntent({ kind: "delete_schedule", schedule_id: "does-not-exist" });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "delete_schedule",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "404", message: "unknown schedule_id" } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_rejected");
    unsubscribe();
  });

  it("resolves as intent_rejected when a late async rejection arrives via onLateIntentRejected (surface-agnostic delivery)", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const ackPromise = mod.submitSystemIntent({ kind: "uninstall_extension", extension_id: "ext-1" });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "uninstall_extension",
    ) as { intent: { intent_id: string } };
    const intentId = sent.intent.intent_id;

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: intentId } as never);
    });
    // Let submitSystemIntent's `.then()` register the late-rejection
    // listener before the second frame arrives (test-harness-only
    // ordering concern, see useProviderSurfaceSocket.test.ts's identical
    // note — a real round trip is macrotask-scale).
    await Promise.resolve();
    await Promise.resolve();
    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: intentId, code: "not_found", message: "extension not installed" } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_rejected");
    if (ack.type === "intent_rejected") {
      expect(ack.code).toBe("not_found");
      expect(ack.message).toBe("extension not installed");
    }
    unsubscribe();
  });

  it("resolves as the original intent_accepted once the late-rejection window elapses with no rejection", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    vi.useFakeTimers();
    try {
      const ackPromise = mod.submitSystemIntent({ kind: "enable_extension", extension_id: "ext-1" });
      expect(ackPromise).not.toBeNull();
      const sent = ws.outbound.find(
        (f) => (f as { intent?: { kind?: string } }).intent?.kind === "enable_extension",
      ) as { intent: { intent_id: string } };

      act(() => {
        ws.emit({ type: "intent_accepted", intent_id: sent.intent.intent_id } as never);
      });
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(2000);

      const ack = await ackPromise!;
      expect(ack.type).toBe("intent_accepted");
    } finally {
      vi.useRealTimers();
    }
    unsubscribe();
  });
});

describe("waitForSystemFrame / submitSystemIntentAwaitingUpsert", () => {
  it("waitForSystemFrame resolves with the first frame matching the predicate", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const promise = mod.waitForSystemFrame(isHarnessProfileUpsert);
    act(() => {
      // Non-matching frame first — must be ignored, not resolved on.
      ws.emit({ type: "node_removed", cv: 1, node_id: "n1" } as never);
      ws.emit({
        type: "harness_profile_upsert",
        cv: 2,
        profile: {
          harness_profile_id: "p1", cv: 2, display: "P1", is_default: false,
          disabled_builtin_extensions: [], disabled_builtin_tools: [], config_schema: null,
        },
        intent_id: null,
      } as never);
    });

    const frame = await promise;
    expect(frame?.type).toBe("harness_profile_upsert");
    expect(frame?.profile.harness_profile_id).toBe("p1");
    unsubscribe();
  });

  it("waitForSystemFrame resolves undefined once the timeout elapses with no match", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    vi.useFakeTimers();
    try {
      const promise = mod.waitForSystemFrame(isHarnessProfileUpsert, 500);
      await vi.advanceTimersByTimeAsync(600);
      expect(await promise).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
    unsubscribe();
  });

  it("submitSystemIntentAwaitingUpsert returns null when the connection is not open", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const result = await mod.submitSystemIntentAwaitingUpsert(
      { kind: "save_harness_profile", harness_profile_id: null, config: {}, revision: null, writes: [] } as never,
      matchesHarnessProfileUpsertIntent,
    );
    expect(result).toBeNull();
  });

  it("resolves {ok:true, frame} once the intent_id-matching upsert arrives", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const resultPromise = mod.submitSystemIntentAwaitingUpsert(
      { kind: "save_harness_profile", harness_profile_id: null, config: { name: "New" }, revision: null, writes: [] } as never,
      matchesHarnessProfileUpsertIntent,
    );
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "save_harness_profile",
    ) as { intent: { intent_id: string } };
    const intentId = sent.intent.intent_id;

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: intentId } as never);
    });
    // Let the ack's own .then() settle before the upsert arrives (same
    // test-harness-only ordering note `submitSystemIntent`'s own late-
    // rejection tests above carry).
    await Promise.resolve();
    await Promise.resolve();
    act(() => {
      ws.emit({
        type: "harness_profile_upsert",
        cv: 2,
        profile: {
          harness_profile_id: "new-1", cv: 2, display: "New", is_default: false,
          disabled_builtin_extensions: [], disabled_builtin_tools: [], config_schema: null,
        },
        intent_id: intentId,
      } as never);
    });

    const result = await resultPromise;
    expect(result).not.toBeNull();
    if (result && result.ok) {
      expect(result.frame?.profile.harness_profile_id).toBe("new-1");
    } else {
      throw new Error(`expected ok:true, got ${JSON.stringify(result)}`);
    }
    unsubscribe();
  });

  it("resolves {ok:false, code, message} on rejection without waiting for a frame", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const unsubscribe = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const resultPromise = mod.submitSystemIntentAwaitingUpsert(
      { kind: "save_harness_profile", harness_profile_id: null, config: { name: "New" }, revision: null, writes: [] } as never,
      matchesHarnessProfileUpsertIntent,
    );
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "save_harness_profile",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "invalid", message: "bad name" } as never);
    });

    const result = await resultPromise;
    expect(result).toEqual({ ok: false, code: "invalid", message: "bad name" });
    unsubscribe();
  });
});

describe("subscribeSystemSocketConnection", () => {
  it("fires synchronously with the current state on subscribe", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/systemFeedRegistry");
    const states: boolean[] = [];
    const unsubscribeFrames = mod.subscribeSystemFrames(() => {});
    await vi.waitFor(() => expect(mod.isSystemSocketOpen()).toBe(true));

    const unsubscribeConn = mod.subscribeSystemSocketConnection((open) => states.push(open));
    expect(states).toEqual([true]);

    unsubscribeConn();
    unsubscribeFrames();
  });
});
