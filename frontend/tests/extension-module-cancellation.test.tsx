import { render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../src/components/extensionModuleLoader", () => ({
  loadExtensionModule: vi.fn(() => new Promise(() => {})),
  disposeExtensionModules: vi.fn(),
}));
vi.mock("../src/lib/frontendLogger", () => ({ logDurable: vi.fn() }));

import { ExtensionModuleSlot } from "../src/components/ExtensionSlots";
import { loadExtensionModule } from "../src/components/extensionModuleLoader";

class FakeObserver {
  static instances: FakeObserver[] = [];
  disconnected = 0;
  constructor() { FakeObserver.instances.push(this); }
  observe() {}
  disconnect() { this.disconnected += 1; }
  takeRecords() { return []; }
}

beforeEach(() => {
  vi.mocked(loadExtensionModule).mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  FakeObserver.instances = [];
});

it("releases the shared long-task observer when unmounted during a pending import", async () => {
  vi.stubGlobal("PerformanceObserver", FakeObserver);
  const view = render(<ExtensionModuleSlot module={{
    extension_id: "ext.pending",
    extension_name: "Pending",
    slot: "composer-actions",
    id: "pending",
    label: "Pending",
    kind: "module",
    module_url: "/api/extensions/ext.pending/frontend/pending.js?v=1",
    payments: false,
  }} />);

  await vi.waitFor(() => expect(FakeObserver.instances).toHaveLength(1));
  view.unmount();
  expect(FakeObserver.instances[0].disconnected).toBe(1);
});

it("exposes pending state and cancels a deferred responsive mount before its frame", () => {
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    frames.push(callback);
    return frames.length;
  }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const view = render(<ExtensionModuleSlot module={{
    extension_id: "ext.responsive",
    extension_name: "Responsive",
    slot: "right-panel-canvas",
    id: "responsive",
    label: "Responsive",
    kind: "module",
    module_url: "/api/extensions/ext.responsive/frontend/responsive.js?v=1",
    payments: false,
  }} />);

  expect(view.container.querySelector("[aria-busy='true']")).toBeTruthy();
  view.unmount();
  frames.shift()?.(0);
  expect(loadExtensionModule).not.toHaveBeenCalled();
});
