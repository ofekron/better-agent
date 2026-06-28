import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import React from "react";

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

beforeEach(() => {
  installMemoryStorage();
});

afterEach(() => {
  cleanup();
});

// Stub the heavy / browser-API-hostile components. The harness drives
// the chat surface — file viewer, monaco, markdown preview, modals,
// rearranger viewers, etc. are out of scope and would otherwise pull
// in megabytes of code or touch APIs happy-dom doesn't support.

vi.mock("@monaco-editor/react", () => ({
  default: () => null,
  Editor: () => null,
  DiffEditor: () => null,
}));

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
vi.mock("../src/components/RearrangerTreeView", () => ({
  RearrangerTreeView: () => null,
}));
vi.mock("../src/components/RewindPopover", () => ({ RewindPopover: () => null }));
vi.mock("../src/components/TraceViewer", () => ({ TraceViewer: () => null }));

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
