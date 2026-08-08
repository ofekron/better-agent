import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import i18n from "i18next";
import React from "react";
import { initReactI18next } from "react-i18next";

// happy-dom 20 doesn't ship a localStorage by default. Provide a tiny
// in-memory polyfill so App's `useState(() => localStorage.getItem(...))`
// initializers don't throw on mount.
class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length() { return this.map.size; }
  clear(): void { this.map.clear(); }
  getItem(key: string): string | null {
    return this.map.has(key) ? this.map.get(key)! : null;
  }
  key(index: number): string | null {
    return Array.from(this.map.keys())[index] ?? null;
  }
  removeItem(key: string): void { this.map.delete(key); }
  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }
}

function installMemoryStorage() {
  Object.defineProperty(globalThis, "localStorage", {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
  });
  Object.defineProperty(globalThis, "sessionStorage", {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
  });
}

installMemoryStorage();

await i18n
  .use(initReactI18next)
  .init({
    lng: "en",
    fallbackLng: false,
    resources: {},
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
  });

beforeEach(() => {
  installMemoryStorage();
  // Chat Surface Contract v2 (frontend/src/adapter/flag.ts) defaults ON.
  // The existing suite is written against the legacy render path, so pin
  // the kill-switch here globally; a v2-specific suite (e.g. flag.test.ts)
  // clears/overrides it in its own beforeEach to exercise the true default.
  localStorage.setItem("ba.surface_v2", "0");
  // Native Contract-Node surface (frontend/src/surface/flag.ts) defaults
  // ON as of Phase I stage 2b — production's sole chat content plane. The
  // existing suite (TurnGroup/MessageBubble via renderApp's fixture-seeded
  // `tree.messages`, and messages_replay/messages_delta/agent_message
  // frame emission through the legacy `/ws/chat` mock) is written against
  // that pre-existing render path, so pin the kill-switch here globally
  // too, same pattern as ba.surface_v2 above; a native-specific suite
  // (surface/*.test.ts) overrides it in its own setup to exercise the
  // true default.
  localStorage.setItem("ba.surface_native", "0");
  // Route state leaks across tests otherwise: a prior test's /s/<id> URL
  // makes App's route-sync effect fetch/select that session in the next
  // test's fresh mock backend.
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
});

// Stub the heavy / browser-API-hostile components. The harness drives
// the chat surface — file viewer, monaco, markdown preview, modals,
// etc. are out of scope and would otherwise pull
// in megabytes of code or touch APIs happy-dom doesn't support.

// Extension frontend modules are backend-served assets the node runtime
// cannot dynamically import; resolve them to the harness contract doubles.
// A test file's own vi.mock of the loader overrides this default.
vi.mock("../src/components/extensionModuleLoader", async () => {
  const stubs = await import("./harness/extensionModuleStubs");
  return { loadExtensionModule: stubs.loadStubExtensionModule };
});

vi.mock("@uiw/react-markdown-preview", () => ({
  default: ({ source }: { source?: string }) =>
    React.createElement("div", { "data-test-md": "true" }, source ?? ""),
}));

vi.mock("react-markdown", () => ({
  default: ({ children }: { children?: string }) =>
    React.createElement("div", { "data-test-md": "true" }, children ?? ""),
}));

vi.mock("../src/components/FileTree", () => ({ FileTree: () => null }));
vi.mock("../src/components/FileViewer", () => ({ FileViewer: () => null }));
vi.mock("../src/components/SetupModal", () => ({ SetupModal: () => null }));
vi.mock("../src/components/DirPickerModal", () => ({ DirPickerModal: () => null }));
vi.mock("../src/components/SelectionPopup", () => ({ SelectionPopup: () => null }));
vi.mock("../src/components/RewindPopover", () => ({ RewindPopover: () => null }));

// happy-dom doesn't implement scrollTo / scrollIntoView in a useful
// way; stub so components that auto-scroll don't throw.
if (typeof Element !== "undefined") {
  Element.prototype.scrollIntoView = function () {};
  Object.defineProperty(Element.prototype, "scrollTop", {
    get: () => 0,
    set: () => {},
    configurable: true,
  });
  Object.defineProperty(Element.prototype, "scrollHeight", {
    get: () => 0,
    configurable: true,
  });
}
