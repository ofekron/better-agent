import { afterEach, describe, expect, it, vi } from "vitest";
import { disposeSharedSnapshotScope, scopedSnapshotKey, sharedSnapshotPoller, SharedSnapshotPoller } from "../src/lib/sharedSnapshotPoller";

afterEach(() => vi.restoreAllMocks());

describe("SharedSnapshotPoller", () => {
  it("coalesces and throttles 100 requests", async () => {
    let resolve!: (value: number) => void;
    const load = vi.fn(() => new Promise<number>((done) => { resolve = done; }));
    const poller = new SharedSnapshotPoller({ load, minIntervalMs: 1000, now: () => 10 });
    const requests = Array.from({ length: 100 }, () => poller.request());
    expect(load).toHaveBeenCalledTimes(1);
    resolve(1);
    await Promise.all(requests);
    await poller.request();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("suppresses hidden requests", async () => {
    const original = Object.getOwnPropertyDescriptor(document, "hidden");
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    try {
      const load = vi.fn(async () => 1);
      await new SharedSnapshotPoller({ load, minIntervalMs: 10 }).request();
      expect(load).not.toHaveBeenCalled();
    } finally {
      if (original) Object.defineProperty(document, "hidden", original);
      else delete (document as unknown as { hidden?: boolean }).hidden;
    }
  });

  it("backs off failures with bounded jitter", async () => {
    let now = 100;
    const load = vi.fn(async () => { throw new Error("offline"); });
    const poller = new SharedSnapshotPoller({ load, minIntervalMs: 1000, maxBackoffMs: 4000, now: () => now, random: () => 0.5 });
    await poller.request();
    now = 1099;
    await poller.request();
    expect(load).toHaveBeenCalledTimes(1);
    now = 1100;
    await poller.request();
    expect(load).toHaveBeenCalledTimes(2);
    expect(poller.metrics.backoffs).toBe(2);
  });

  it("rejects stale revisions and REST completions superseded by WS", async () => {
    let resolve!: (value: { authority_epoch: string; revision: number; value: string }) => void;
    const poller = new SharedSnapshotPoller({
      load: () => new Promise((done) => { resolve = done; }), minIntervalMs: 0,
    });
    const request = poller.request();
    poller.publish({ authority_epoch: "epoch-a", revision: 2, value: "ws" });
    resolve({ authority_epoch: "epoch-a", revision: 1, value: "rest" });
    await request;
    expect(poller.current()).toMatchObject({ value: "ws" });
    poller.publish({ authority_epoch: "epoch-b", revision: 0, value: "restart" });
    poller.publish({ authority_epoch: "epoch-a", revision: 99, value: "retired" });
    expect(poller.current()).toMatchObject({ value: "restart" });
  });

  it("disposes one auth scope without touching another principal", () => {
    const load = vi.fn(async () => 1);
    const first = sharedSnapshotPoller(scopedSnapshotKey("/api", "a", "domain"), { load, minIntervalMs: 1 });
    const second = sharedSnapshotPoller(scopedSnapshotKey("/api", "b", "domain"), { load, minIntervalMs: 1 });
    disposeSharedSnapshotScope("a");
    expect(sharedSnapshotPoller(scopedSnapshotKey("/api", "a", "domain"), { load, minIntervalMs: 1 })).not.toBe(first);
    expect(sharedSnapshotPoller(scopedSnapshotKey("/api", "b", "domain"), { load, minIntervalMs: 1 })).toBe(second);
  });
});
