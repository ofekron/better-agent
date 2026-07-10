import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { switchStateTestApi } from "../../extensions/switch-control/ui/switch.entry.js";
import { workerStoreTestApi } from "../../better-agent-private/extensions/team-orchestration/ui/team-sidebar.entry.js";

beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
afterEach(() => { vi.unstubAllGlobals(); switchStateTestApi.reset(); workerStoreTestApi.reset(); });

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
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ switchable: true }) } as Response);
    const store = switchStateTestApi.stateStore("http://api");
    const seen: unknown[] = [];
    store.listeners.add((value: unknown) => seen.push(value));
    await Promise.all(Array.from({ length: 100 }, () => switchStateTestApi.refreshState("http://api", store)));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([{ switchable: true }]);
  });
});
