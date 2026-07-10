import { act, cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pendingRefreshTestApi, usePendingNodes } from "../../better-agent-private/extensions/machine-nodes/ui/machine-nodes.entry.js";

function PendingProbe({ apiBaseUrl }: { apiBaseUrl: string }) {
  const { pending } = usePendingNodes({ apiBaseUrl, authScopeKey: "principal-a" }, React, "authed");
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
    expect(fetch).toHaveBeenCalledTimes(1);
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
  });
});
