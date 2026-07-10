import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { switchStateTestApi } from "../../extensions/switch-control/ui/switch.entry.js";
import { workerStoreTestApi } from "../../better-agent-private/extensions/team-orchestration/ui/team-sidebar.entry.js";

beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); switchStateTestApi.reset(); workerStoreTestApi.reset(); });

describe("extension URL-keyed stores", () => {
  it("coalesces team-worker snapshots across slots", async () => {
    let resolve!: (value: Response) => void;
    vi.mocked(fetch).mockReturnValue(new Promise((done) => { resolve = done; }));
    const store = workerStoreTestApi.workerStore("http://api:/repo");
    const requests = Array.from({ length: 100 }, () => workerStoreTestApi.refreshWorkers("http://api", "/repo", store));
    expect(fetch).toHaveBeenCalledTimes(1);
    resolve({ ok: true, json: async () => ({ workers: [{ id: "w" }] }) } as Response);
    await Promise.all(requests);
    expect(store.workers).toEqual([{ id: "w" }]);
  });

  it("coalesces switch snapshots and publishes one result to every slot", async () => {
    vi.spyOn(Date, "now").mockReturnValue(31_000);
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ authority_epoch: "a", revision: 1, data: { switchable: true } }) } as Response);
    const store = switchStateTestApi.stateStore("http://api");
    const seen: unknown[] = [];
    store.listeners.add((value: unknown) => seen.push(value));
    await Promise.all(Array.from({ length: 100 }, () => switchStateTestApi.refreshState("http://api", store)));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([{ switchable: true }]);
  });

  it("rejects an old Switch REST completion after newer WS state", async () => {
    vi.spyOn(Date, "now").mockReturnValue(31_000);
    let resolve!: (value: Response) => void;
    vi.mocked(fetch).mockReturnValue(new Promise((done) => { resolve = done; }));
    const store = switchStateTestApi.stateStore("switch-race");
    const pending = switchStateTestApi.refreshState("http://api", store);
    expect(switchStateTestApi.publishStateEnvelope(store, {
      authority_epoch: "epoch", revision: 2, data: { line: "new" },
    })).toBe(true);
    resolve({ ok: true, json: async () => ({ authority_epoch: "epoch", revision: 1, data: { line: "old" } }) } as Response);
    await pending;
    expect(store.value).toEqual({ line: "new" });
    expect(switchStateTestApi.publishStateEnvelope(store, { data: { line: "unversioned" } })).toBe(false);
  });

  it("evicts idle Switch stores and disposes logout scope immediately", () => {
    vi.useFakeTimers();
    const idleKey = JSON.stringify(["http://api", "idle", "switch-control"]);
    const unsubscribe = switchStateTestApi.subscribeState("http://api", () => {}, idleKey);
    unsubscribe();
    vi.advanceTimersByTime(60_000);
    expect(switchStateTestApi.has(idleKey)).toBe(false);
    const logoutKey = JSON.stringify(["http://api", "logout", "switch-control"]);
    switchStateTestApi.stateStore(logoutKey);
    const before = switchStateTestApi.size();
    window.dispatchEvent(new CustomEvent("extension_auth_scope_disposed", { detail: { authScopeKey: "logout" } }));
    expect(switchStateTestApi.size()).toBe(before - 1);
  });
});
