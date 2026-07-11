import { act, cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pendingRefreshTestApi, usePendingNodes } from "../../better-agent-private/extensions/machine-nodes/ui/machine-nodes.entry.js";

function PendingProbe({ apiBaseUrl, authScopeKey = "principal-a" }: { apiBaseUrl: string; authScopeKey?: string }) {
  const { pending } = usePendingNodes({ apiBaseUrl, authScopeKey }, React, "authed");
  return <output>{pending.map((item: { node_id: string }) => item.node_id).join(",")}</output>;
}

beforeEach(() => {
  pendingRefreshTestApi.reset();
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({ authority_epoch: "epoch", revision: 0, data: { pending_nodes: [] } }),
  })));
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.unstubAllGlobals();
});

describe("machine-nodes pending projection", () => {
  it("throttles 100 refresh requests inside the minimum interval", async () => {
    const store = pendingRefreshTestApi.pendingStore("http://throttle");
    const now = () => 10_000;
    await Promise.all(Array.from({ length: 100 }, () =>
      pendingRefreshTestApi.refreshPending("http://throttle", store, { now, random: () => 0.5 }),
    ));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(store.metrics.attempts).toBe(1);
    expect(store.metrics.coalesced + store.metrics.suppressed).toBe(99);
    expect(store.metrics.maxOutstanding).toBe(1);
    expect(store.metrics.triggers.manual).toBe(100);
  });

  it("keeps one producer through StrictMode, multiple slots, and fresh contexts", async () => {
    const diagnostics: Array<Record<string, unknown>> = [];
    const onDiagnostic = (event: Event) => diagnostics.push((event as CustomEvent).detail);
    window.addEventListener("better-agent:extension-performance", onDiagnostic);
    try {
      const view = render(
        <React.StrictMode>
          <PendingProbe apiBaseUrl="http://strict" />
          <PendingProbe apiBaseUrl="http://strict" />
          <PendingProbe apiBaseUrl="http://strict" />
        </React.StrictMode>,
      );
      await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
      for (let index = 0; index < 100; index += 1) {
        view.rerender(
          <React.StrictMode>
            <PendingProbe apiBaseUrl="http://strict" />
            <PendingProbe apiBaseUrl="http://strict" />
            <PendingProbe apiBaseUrl="http://strict" />
          </React.StrictMode>,
        );
      }
      expect(fetch).toHaveBeenCalledTimes(1);
      const key = JSON.stringify([
        "http://strict/api/extensions/ofek-dev.machine-nodes/backend", "principal-a", "pending-nodes",
      ]);
      const store = pendingRefreshTestApi.pendingStore(key);
      expect(store.metrics.maxOutstanding).toBe(1);
      expect(store.metrics.maxSubscribers).toBe(3);
      expect(diagnostics.some((item) => item.stage === "pending.refresh_started")).toBe(true);
      expect(JSON.stringify(diagnostics)).not.toContain("principal-a");
      expect(JSON.stringify(diagnostics)).not.toContain("http://strict");
    } finally {
      window.removeEventListener("better-agent:extension-performance", onDiagnostic);
    }
  });

  it("suppresses hidden refresh and performs one stale resume refresh", async () => {
    const originalHidden = Object.getOwnPropertyDescriptor(document, "hidden");
    const store = pendingRefreshTestApi.pendingStore("http://resume");
    try {
      Object.defineProperty(document, "hidden", { configurable: true, value: true });
      await pendingRefreshTestApi.refreshPending("http://resume", store, { now: () => 1, random: () => 0.5 });
      expect(fetch).not.toHaveBeenCalled();
      Object.defineProperty(document, "hidden", { configurable: true, value: false });
      await pendingRefreshTestApi.refreshPending("http://resume", store, { now: () => 31_001, random: () => 0.5 });
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally {
      if (originalHidden) Object.defineProperty(document, "hidden", originalHidden);
    }
  });

  it("applies bounded exponential failure backoff", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 503 } as Response);
    const store = pendingRefreshTestApi.pendingStore("http://backoff");
    await pendingRefreshTestApi.refreshPending("http://backoff", store, { now: () => 1000, random: () => 0.5 });
    expect(store.nextAttemptAt).toBe(31_000);
    await pendingRefreshTestApi.refreshPending("http://backoff", store, { now: () => 31_000, random: () => 0.5 });
    expect(store.nextAttemptAt).toBe(91_000);
    expect(store.nextAttemptAt - 31_000).toBeLessThanOrEqual(300_000);
  });

  it("recovers after a slow rejected request and coalesces reconnect triggers", async () => {
    let rejectFirst!: (reason: Error) => void;
    vi.mocked(fetch).mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
      rejectFirst = reject;
      expect((options as RequestInit).signal).toBeInstanceOf(AbortSignal);
    })).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ authority_epoch: "epoch", revision: 0, data: { pending_nodes: [] } }),
    } as Response);
    const store = pendingRefreshTestApi.pendingStore("http://reconnect");
    const first = pendingRefreshTestApi.refreshPending("http://reconnect", store, {
      now: () => 1, random: () => 0.5, trigger: "online",
    });
    const coalesced = Array.from({ length: 20 }, () => pendingRefreshTestApi.refreshPending(
      "http://reconnect", store, { now: () => 1, random: () => 0.5, trigger: "online" },
    ));
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(store.metrics.maxOutstanding).toBe(1);
    rejectFirst(new Error("offline"));
    await Promise.all([first, ...coalesced]);
    await pendingRefreshTestApi.refreshPending("http://reconnect", store, {
      now: () => 30_001, random: () => 0.5, trigger: "online",
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(store.metrics.maxOutstanding).toBe(1);
    expect(store.metrics.triggers.online).toBe(22);
  });

  it("recovers lifecycle counters when fetch throws synchronously", async () => {
    vi.mocked(fetch).mockImplementationOnce(() => { throw new Error("sync fetch failure"); })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ authority_epoch: "epoch", revision: 0, data: { pending_nodes: [] } }),
      } as Response);
    const store = pendingRefreshTestApi.pendingStore("http://sync-throw");
    await pendingRefreshTestApi.refreshPending("http://sync-throw", store, { now: () => 1, random: () => 0.5 });
    expect(store.inFlight).toBeNull();
    expect(store.metrics.outstanding).toBe(0);
    await pendingRefreshTestApi.refreshPending("http://sync-throw", store, {
      now: () => 30_001, random: () => 0.5,
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(store.metrics.maxOutstanding).toBe(1);
  });

  it("does not start a deferred request after immediate version replacement", async () => {
    const store = pendingRefreshTestApi.pendingStore("http://immediate-replace");
    const request = pendingRefreshTestApi.refreshPending("http://immediate-replace", store, { now: () => 1 });
    pendingRefreshTestApi.installOwner();
    await request;
    expect(fetch).not.toHaveBeenCalled();
    expect(store.metrics.cancellations).toBe(1);
    expect(store.metrics.outstanding).toBe(0);
    expect(store.inFlight).toBeNull();
  });

  it("ignores 100 equivalent fresh contexts and refetches for a semantic URL change", async () => {
    const { rerender } = render(<PendingProbe apiBaseUrl="http://one" />);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    for (let index = 0; index < 100; index += 1) {
      rerender(<PendingProbe apiBaseUrl="http://one" />);
    }
    expect(fetch).toHaveBeenCalledTimes(1);

    rerender(<PendingProbe apiBaseUrl="http://two" />);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });

  it("shares cadence across remounts and creates one new generation per auth or API change", async () => {
    vi.useFakeTimers();
    const first = render(<PendingProbe apiBaseUrl="http://scope" authScopeKey="principal-a" />);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(1);
    first.unmount();
    const remount = render(<PendingProbe apiBaseUrl="http://scope" authScopeKey="principal-a" />);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(1);
    remount.rerender(<PendingProbe apiBaseUrl="http://scope" authScopeKey="principal-b" />);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(2);
    remount.rerender(<PendingProbe apiBaseUrl="http://other" authScopeKey="principal-b" />);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("shares one in-flight snapshot across multiple slots", async () => {
    let resolveFetch!: (value: unknown) => void;
    vi.mocked(fetch).mockImplementation(() => new Promise((resolve) => { resolveFetch = resolve; }));
    render(
      <>
        <PendingProbe apiBaseUrl="http://multi" />
        <PendingProbe apiBaseUrl="http://multi" />
        <PendingProbe apiBaseUrl="http://multi" />
      </>,
    );
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    resolveFetch({ ok: true, json: async () => ({ authority_epoch: "epoch", revision: 0, data: { pending_nodes: [] } }) });
    await vi.waitFor(() => expect(screen.getAllByRole("status")).toHaveLength(3));
  });

  it("projects requested and resolved events into every slot without refetching", async () => {
    render(
      <>
        <PendingProbe apiBaseUrl="http://events" />
        <PendingProbe apiBaseUrl="http://events" />
      </>,
    );
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await act(async () => {});

    act(() => {
      window.dispatchEvent(new CustomEvent("node_registration_requested", {
        detail: { node_id: "node-1", apiBaseUrl: "http://events", authScopeKey: "principal-a", authority_epoch: "epoch", revision: 1 },
      }));
    });
    await vi.waitFor(() => expect(screen.getAllByText("node-1")).toHaveLength(2));
    expect(fetch).toHaveBeenCalledTimes(1);

    act(() => {
      window.dispatchEvent(new CustomEvent("node_registration_resolved", {
        detail: { node_id: "node-1", apiBaseUrl: "http://events", authScopeKey: "principal-a", authority_epoch: "epoch", revision: 2 },
      }));
    });
    await vi.waitFor(() => expect(screen.queryByText("node-1")).toBeNull());
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects an old pending REST completion after a newer DOM event", async () => {
    let resolve!: (value: Response) => void;
    vi.mocked(fetch).mockReturnValue(new Promise((done) => { resolve = done; }));
    const store = pendingRefreshTestApi.pendingStore("machine-race");
    const pending = pendingRefreshTestApi.refreshPending("http://api", store, { now: () => 1 });
    expect(pendingRefreshTestApi.acceptPendingAuthority(store, { authority_epoch: "epoch", revision: 2 })).toBe(true);
    store.pending = [{ node_id: "new" }];
    resolve({ ok: true, json: async () => ({
      authority_epoch: "epoch", revision: 1, data: { pending_nodes: [{ node_id: "old" }] },
    }) } as Response);
    await pending;
    expect(store.pending).toEqual([{ node_id: "new" }]);
    expect(pendingRefreshTestApi.publishPendingEnvelope(store, { data: { pending_nodes: [] } })).toBe(false);
  });

  it("rejects stale and retired WS authority without starting a request", async () => {
    render(<PendingProbe apiBaseUrl="http://ws" />);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const key = JSON.stringify([
      "http://ws/api/extensions/ofek-dev.machine-nodes/backend", "principal-a", "pending-nodes",
    ]);
    const store = pendingRefreshTestApi.pendingStore(key);
    await vi.waitFor(() => expect(store.authority).toEqual({ epoch: "epoch", revision: 0 }));
    const baseline = vi.mocked(fetch).mock.calls.length;
    act(() => {
      window.dispatchEvent(new CustomEvent("node_registration_requested", { detail: {
        node_id: "new", apiBaseUrl: "http://ws", authScopeKey: "principal-a",
        authority_epoch: "epoch-new", revision: 2,
      } }));
      window.dispatchEvent(new CustomEvent("node_registration_requested", { detail: {
        node_id: "stale", apiBaseUrl: "http://ws", authScopeKey: "principal-a",
        authority_epoch: "epoch-new", revision: 1,
      } }));
      window.dispatchEvent(new CustomEvent("node_registration_requested", { detail: {
        node_id: "retired", apiBaseUrl: "http://ws", authScopeKey: "principal-a",
        authority_epoch: "epoch", revision: 99,
      } }));
    });
    await vi.waitFor(() => expect(screen.getByRole("status").textContent).toBe("new"));
    expect(screen.getByRole("status").textContent).not.toContain("stale");
    expect(screen.getByRole("status").textContent).not.toContain("retired");
    expect(fetch).toHaveBeenCalledTimes(baseline);
  });

  it("disposes Machine stores immediately on logout scope", () => {
    pendingRefreshTestApi.pendingStore(JSON.stringify(["http://api", "logout", "pending-nodes"]));
    const before = pendingRefreshTestApi.size();
    window.dispatchEvent(new CustomEvent("extension_auth_scope_disposed", { detail: { authScopeKey: "logout" } }));
    expect(pendingRefreshTestApi.size()).toBe(before - 1);
  });

  it("evicts the Machine store after the last slot stays unmounted", () => {
    vi.useFakeTimers();
    const key = JSON.stringify([
      "http://idle/api/extensions/ofek-dev.machine-nodes/backend", "principal-a", "pending-nodes",
    ]);
    const mounted = render(<PendingProbe apiBaseUrl="http://idle" />);
    expect(pendingRefreshTestApi.has(key)).toBe(true);
    mounted.unmount();
    vi.advanceTimersByTime(60_000);
    expect(pendingRefreshTestApi.has(key)).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("disposes the old owner request, stores, listener, and timers on extension version replacement", async () => {
    vi.useFakeTimers();
    let aborted = false;
    vi.mocked(fetch).mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      (options as RequestInit).signal?.addEventListener("abort", () => {
        aborted = true;
        reject(new DOMException("aborted", "AbortError"));
      });
    }));
    const mounted = render(<PendingProbe apiBaseUrl="http://version-one" />);
    const key = JSON.stringify([
      "http://version-one/api/extensions/ofek-dev.machine-nodes/backend", "principal-a", "pending-nodes",
    ]);
    const store = pendingRefreshTestApi.pendingStore(key);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(1);
    const request = store.inFlight;
    mounted.unmount();
    pendingRefreshTestApi.installOwner();
    await request;
    expect(aborted).toBe(true);
    expect(pendingRefreshTestApi.size()).toBe(0);
    expect(store.metrics.cancellations).toBe(1);
    expect(store.metrics.failures).toBe(0);
    expect(pendingRefreshTestApi.size()).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
