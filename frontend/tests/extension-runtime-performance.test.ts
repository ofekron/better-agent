import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../src/lib/frontendLogger", () => ({ logDurable: vi.fn() }));

import { logDurable } from "../src/lib/frontendLogger";
import {
  beginExtensionMountWindow,
  disposeExtensionRuntime,
  scheduleExtensionMount,
} from "../src/components/extensionRuntimePerformance";

class FakeObserver {
  static instances: FakeObserver[] = [];
  readonly callback: (list: { getEntries: () => PerformanceEntry[] }) => void;
  disconnected = 0;

  constructor(callback: (list: { getEntries: () => PerformanceEntry[] }) => void) {
    this.callback = callback;
    FakeObserver.instances.push(this);
  }

  observe() {}
  disconnect() { this.disconnected += 1; }
  takeRecords() { return [] as PerformanceEntry[]; }
  emit(entries: PerformanceEntry[]) { this.callback({ getEntries: () => entries }); }
}

let frames: FrameRequestCallback[];
let now: number;

beforeEach(() => {
  frames = [];
  now = 0;
  FakeObserver.instances = [];
  vi.mocked(logDurable).mockClear();
  vi.stubGlobal("PerformanceObserver", FakeObserver);
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    frames.push(callback);
    return frames.length;
  }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(performance, "now").mockImplementation(() => now);
});

afterEach(() => {
  disposeExtensionRuntime("scope-test");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("attributes one concurrent long task once through one scoped observer", () => {
  const finishA = beginExtensionMountWindow("scope-test", "ext/a");
  const finishB = beginExtensionMountWindow("scope-test", "ext/b");
  expect(FakeObserver.instances).toHaveLength(1);

  FakeObserver.instances[0].emit([{ startTime: 1, duration: 80 } as PerformanceEntry]);
  expect(logDurable).toHaveBeenCalledTimes(1);
  expect(logDurable).toHaveBeenCalledWith("extensions.module", "longtask", expect.objectContaining({
    owners: ["ext/a", "ext/b"], duration_ms: 80,
  }));

  finishA();
  expect(FakeObserver.instances[0].disconnected).toBe(0);
  finishB();
  expect(FakeObserver.instances[0].disconnected).toBe(1);
});

it("honors priority and frame budget and never runs a cancelled mount", () => {
  const ran: string[] = [];
  scheduleExtensionMount("scope-test", "low", 30, () => { ran.push("low"); now += 9; });
  scheduleExtensionMount("scope-test", "high", 0, () => { ran.push("high"); now += 9; });
  const cancel = scheduleExtensionMount("scope-test", "cancelled", 10, () => ran.push("cancelled"));
  cancel();

  expect(frames).toHaveLength(1);
  frames.shift()?.(0);
  expect(ran).toEqual(["high"]);
  expect(frames).toHaveLength(1);
  frames.shift()?.(16);
  expect(ran).toEqual(["high", "low"]);
});
