import { StrictMode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../src/lib/frontendLogger", () => ({ logDurable: vi.fn() }));

import {
  ExtensionAuthScopeProvider,
  useExtensionFrontendCatalog,
} from "../src/components/ExtensionSlots";
import { eventBus } from "../src/lib/eventBus";

const SLOTS = Array.from({ length: 19 }, (_, index) => `slot-${index}`);

function SlotProbe({ slot }: { slot: string }) {
  const catalog = useExtensionFrontendCatalog(slot);
  return <span>{catalog.modules[0]?.id ?? "pending"}</span>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("loads and parses one catalog snapshot for nineteen StrictMode slot subscribers", async () => {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({
    entrypoints: [{
      extension_id: "ext.shared",
      name: "Shared",
      frontend_modules: SLOTS.map((slot, index) => ({
        slot,
        id: `module-${index}`,
        label: `Module ${index}`,
        kind: "module",
        module_url: `/api/extensions/ext.shared/frontend/ui/shared.entry.js?v=1`,
      })),
    }],
  })));
  vi.stubGlobal("fetch", fetchMock);

  render(
    <StrictMode>
      <ExtensionAuthScopeProvider authStatus="authenticated" username="tester">
        {SLOTS.map((slot) => <SlotProbe key={slot} slot={slot} />)}
      </ExtensionAuthScopeProvider>
    </StrictMode>,
  );

  expect(await screen.findByText("module-18")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getAllByText(/^module-/)).toHaveLength(19);
});

it("rejects an older catalog response after an extension-change generation", async () => {
  let resolveFirst!: (response: Response) => void;
  const fetchMock = vi.fn(() => {
    if (fetchMock.mock.calls.length === 1) {
      return new Promise<Response>((resolve) => { resolveFirst = resolve; });
    }
    return Promise.resolve(new Response(JSON.stringify({
      entrypoints: [{ extension_id: "ext.new", name: "New", frontend_modules: [{
        slot: "slot-new", id: "new-module", label: "New", kind: "module",
        module_url: "/api/extensions/ext.new/frontend/new.js?v=2",
      }] }],
    })));
  });
  vi.stubGlobal("fetch", fetchMock);

  render(
    <ExtensionAuthScopeProvider authStatus="authenticated" username="stale-test">
      <SlotProbe slot="slot-new" />
    </ExtensionAuthScopeProvider>,
  );
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  eventBus.publish("extensions_changed", {});
  expect(await screen.findByText("new-module")).toBeTruthy();

  resolveFirst(new Response(JSON.stringify({ entrypoints: [] })));
  await Promise.resolve();
  expect(screen.getByText("new-module")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
