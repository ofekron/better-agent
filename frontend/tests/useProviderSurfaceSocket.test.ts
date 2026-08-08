// Coverage for `src/hooks/useProviderSurfaceSocket.ts` (Package D, ADR
// 0007) — the shared "providers" feed connection — plus the v2 half of
// `useProviderChanged`/`useModelsCatalogChanged`'s dual-subscribe
// migration. `useProviderSurfaceSocket` is a true module-level singleton
// (module-scope `socket`/listener sets), so every test re-imports it
// fresh via `vi.resetModules()` — same isolation pattern
// `tests/useRunSummary.test.tsx` already uses for `runSummaryRegistry`.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocketController } from "./harness/mockWebSocket";

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

describe("subscribeProviderFrames", () => {
  it("opens the shared connection and subscribes to the providers feed", async () => {
    vi.resetModules();
    const { subscribeProviderFrames } = await import("../src/hooks/useProviderSurfaceSocket");
    const received: unknown[] = [];
    const unsubscribe = subscribeProviderFrames((f) => received.push(f));

    await vi.waitFor(() => {
      expect(
        ws.outbound.some(
          (f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
            && (f as { feeds: unknown[] }).feeds.includes("providers"),
        ),
      ).toBe(true);
    });

    act(() => {
      ws.emit({ type: "provider_upsert", cv: 1, descriptor: { provider_id: "p1" }, intent_id: null } as never);
    });
    expect(received).toHaveLength(1);

    unsubscribe();
  });

  it("closes the connection once the last subscriber unsubscribes", async () => {
    vi.resetModules();
    const { subscribeProviderFrames, isProviderSocketOpen } = await import(
      "../src/hooks/useProviderSurfaceSocket"
    );
    const unsubscribe = subscribeProviderFrames(() => {});
    await vi.waitFor(() => expect(isProviderSocketOpen()).toBe(true));
    unsubscribe();
    expect(isProviderSocketOpen()).toBe(false);
  });
});

describe("submitProviderIntent", () => {
  it("returns null (no queueing) when the connection is not open", async () => {
    vi.resetModules();
    const { submitProviderIntent } = await import("../src/hooks/useProviderSurfaceSocket");
    // No subscriber has opened the connection yet.
    expect(submitProviderIntent({ kind: "refresh_models", provider_id: "p1" })).toBeNull();
  });

  it("stamps cv/intent_id/session_id and sends over the shared connection once open", async () => {
    vi.resetModules();
    const mod = await import("../src/hooks/useProviderSurfaceSocket");
    const unsubscribe = mod.subscribeProviderFrames(() => {});
    await vi.waitFor(() => expect(mod.isProviderSocketOpen()).toBe(true));

    mod.submitProviderIntent({ kind: "refresh_models", provider_id: "p1" });

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "refresh_models",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent.provider_id).toBe("p1");
    expect(sent!.intent.session_id).toBeNull();
    expect(typeof sent!.intent.intent_id).toBe("string");
    expect(sent!.intent.intent_id).not.toBe("");

    unsubscribe();
  });

  it("resolves as intent_rejected immediately on a synchronous rejection (no late-window wait)", async () => {
    vi.resetModules();
    const mod = await import("../src/hooks/useProviderSurfaceSocket");
    const unsubscribe = mod.subscribeProviderFrames(() => {});
    await vi.waitFor(() => expect(mod.isProviderSocketOpen()).toBe(true));

    const ackPromise = mod.submitProviderIntent({ kind: "begin_login", provider_id: "p1", flow: "oauth_subscription" });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "begin_login",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "forbidden", message: "loopback only" } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_rejected");
    unsubscribe();
  });

  it("resolves as intent_rejected when a late async rejection frame arrives after admission (D4 error propagation)", async () => {
    vi.resetModules();
    const mod = await import("../src/hooks/useProviderSurfaceSocket");
    const unsubscribe = mod.subscribeProviderFrames(() => {});
    await vi.waitFor(() => expect(mod.isProviderSocketOpen()).toBe(true));

    const ackPromise = mod.submitProviderIntent({ kind: "update_provider", provider_id: "does-not-exist", config_patch: {} });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "update_provider",
    ) as { intent: { intent_id: string } };
    const intentId = sent.intent.intent_id;

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: intentId } as never);
    });
    // Let submitProviderIntent's `.then()` actually register the
    // late-rejection listener before the second frame arrives — a
    // test-harness-only concern (both frames are emitted back-to-back,
    // synchronously); a real backend round trip is macrotask-scale, so
    // this ordering is never a race outside this test.
    await Promise.resolve();
    await Promise.resolve();
    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: intentId, code: "404", message: "provider not found" } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_rejected");
    if (ack.type === "intent_rejected") {
      expect(ack.code).toBe("404");
      expect(ack.message).toBe("provider not found");
    }
    unsubscribe();
  });

  it("resolves as the original intent_accepted once the late-rejection window elapses with no rejection", async () => {
    vi.resetModules();
    const mod = await import("../src/hooks/useProviderSurfaceSocket");
    const unsubscribe = mod.subscribeProviderFrames(() => {});
    await vi.waitFor(() => expect(mod.isProviderSocketOpen()).toBe(true));

    vi.useFakeTimers();
    try {
      const ackPromise = mod.submitProviderIntent({ kind: "suspend_provider", provider_id: "p1", suspended: true });
      expect(ackPromise).not.toBeNull();
      const sent = ws.outbound.find(
        (f) => (f as { intent?: { kind?: string } }).intent?.kind === "suspend_provider",
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

describe("useProviderChanged — dual subscription", () => {
  it("calls cb on the legacy provider_changed broadcast", async () => {
    vi.resetModules();
    const { useProviderChanged } = await import("../src/hooks/useProviderChanged");
    // Dynamically imported AFTER resetModules so it's the SAME eventBus
    // module instance the freshly-imported hook above subscribes through
    // (a stale top-level import would be a different singleton entirely).
    const { eventBus } = await import("../src/lib/eventBus");
    const cb = vi.fn();
    renderHook(() => useProviderChanged(cb));

    act(() => eventBus.publish("provider_changed", {} as never));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("also calls cb on a v2 providers-feed frame", async () => {
    vi.resetModules();
    const { useProviderChanged } = await import("../src/hooks/useProviderChanged");
    const cb = vi.fn();
    renderHook(() => useProviderChanged(cb));

    await vi.waitFor(() => {
      expect(
        ws.outbound.some(
          (f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
            && (f as { feeds: unknown[] }).feeds.includes("providers"),
        ),
      ).toBe(true);
    });

    act(() => {
      ws.emit({ type: "credential_state", cv: 1, provider_id: "p1", config_state: "active" } as never);
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });
});

describe("useModelsCatalogChanged — dual subscription", () => {
  it("calls cb with the provider_id on a v2 model_catalog_changed frame", async () => {
    vi.resetModules();
    const { useModelsCatalogChanged } = await import("../src/hooks/useModelsCatalogChanged");
    const cb = vi.fn();
    renderHook(() => useModelsCatalogChanged(cb));

    await vi.waitFor(() => {
      expect(
        ws.outbound.some(
          (f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
            && (f as { feeds: unknown[] }).feeds.includes("providers"),
        ),
      ).toBe(true);
    });

    act(() => {
      ws.emit({ type: "model_catalog_changed", cv: 1, provider_id: "prov-9" } as never);
    });
    expect(cb).toHaveBeenCalledWith({ provider_id: "prov-9" });
  });

  it("still calls cb on the legacy models_catalog_changed broadcast", async () => {
    vi.resetModules();
    const { useModelsCatalogChanged } = await import("../src/hooks/useModelsCatalogChanged");
    const { eventBus } = await import("../src/lib/eventBus");
    const cb = vi.fn();
    renderHook(() => useModelsCatalogChanged(cb));

    act(() => eventBus.publish("models_catalog_changed", { provider_id: "prov-legacy" }));
    expect(cb).toHaveBeenCalledWith({ provider_id: "prov-legacy" });
  });
});
