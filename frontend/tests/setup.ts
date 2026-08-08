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

beforeEach(async () => {
  installMemoryStorage();
  // `utils/uiSelection.ts` holds module-level caches (selected project,
  // remembered sessions, open tabs) — same singleton class as the
  // `sessionSurfaceRegistry` stub below, but tests exercise its REAL
  // behavior, so a reset hook replaces the stub pattern. Reseed from the
  // fresh MemoryStorage just installed so one test's selection never leaks
  // into the next.
  // try/catch, not optional chaining: a test file that vi.mocks the module
  // gets a mock that THROWS on accessing an export the factory didn't
  // define — and a mock has no leaking real state to reset anyway.
  try {
    (await import("../src/utils/uiSelection")).__resetUiSelectionForTests();
  } catch {
    // module mocked by the current test file
  }
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

afterEach(async () => {
  cleanup();
  // `utils/writeBacklog.ts`'s queue + in-flight sweep are module-level
  // singletons: a PATCH queued in this test (e.g. uiSelection write-through)
  // would otherwise fire during the next test's window and — landing between
  // two renderApp() MockBackend installs — escape to the real fetch
  // (happy-dom resolves it against http://localhost:3000). Abandon the sweep
  // and clear the queue.
  // Same vi.mock escape hatch as the uiSelection reset in beforeEach above.
  try {
    (await import("../src/utils/writeBacklog")).__resetWriteBacklogForTests();
  } catch {
    // module mocked by the current test file
  }
  mockFoldersV2State = [];
  mockTagsV2State = [];
  mockModelFacetState = [];
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

// ADR 0008 (Package B): `lib/sessionSurfaceRegistry.ts` is a true
// module-level singleton (like `hooks/useProviderSurfaceSocket.ts`/`lib/
// runSummaryRegistry.ts`) that lazily opens a real `/ws/v2/surface`
// WebSocket + fires REST hydration on first use — `SessionList.tsx`
// (mounted in nearly every `renderApp()` test) calls its `useFoldersV2`/
// `useTagsV2` hooks unconditionally, so leaving the real module wired
// would open that socket in effectively every test in this suite. Unlike
// `runSummaryRegistry` (read-only, no blocking await anywhere), `useSession
// .ts`'s `archiveSession`/`renameSession`/`markSessionOpened` AWAIT its
// `submitIntent()` result — with no ack simulation wired into
// `MockWebSocketController` (by design: `useProviderSurfaceSocket.test.ts`/
// `settings-page-provider-intent-routing.test.tsx` simulate acks
// EXPLICITLY, per-test, and a global auto-ack would break that precise
// control) that leaves the promise permanently unsettled, and the
// singleton has no reset hook a shared `renderApp()`-based suite could
// call between tests (a stale socket from an earlier test's now-torn-down
// `MockWebSocketController` leaks into every later test in the same
// file). Stub it here — same rationale/pattern as `FileViewer`/
// `SetupModal` above — so the broad suite exercises the pre-migration
// REST-fallback behavior unchanged (`submitIntent` returning `null` is
// `SurfaceSocket.submit`'s own documented "not open" contract, so
// `trySessionIntentV2` correctly falls back to legacy REST every time).
// A dedicated test file that wants the REAL v2 session-plane behavior
// overrides this via its own `vi.mock`/`vi.doMock`, same escape hatch the
// extensionModuleLoader stub above documents.
//
// `useFoldersV2`/`useTagsV2` read from module-scope state a test can seed
// via `__setMockSessionOrganizationV2` (imported from this same mocked
// module) — for the handful of pre-existing tests that assert on the
// folder/tag catalog SessionList.tsx now sources from v2 instead of the
// legacy `GET /api/session-organization` snapshot. Reset in the global
// `afterEach` below so one test's seed never leaks into the next.
let mockFoldersV2State: unknown[] = [];
let mockTagsV2State: unknown[] = [];
// B11 (parity audit): the "Model" filter facet, migrated to this same v2
// singleton (`useModelFacetV2`) off the legacy `GET /api/session-
// organization` `models` field — seeded via `__setMockSessionOrganizationV2`'s
// own `models` key, same shape that snapshot already carried.
let mockModelFacetState: string[] = [];
vi.mock("../src/lib/sessionSurfaceRegistry", () => ({
  sessionSurfaceRegistry: {
    submitIntent: () => null,
    // Create-then-correlate mutations (`SessionList.tsx`'s
    // `createAndAssignFolder`/`createAndAssignTag`) treat a `null` return
    // exactly like `submitIntent`'s own not-open contract — REST fallback.
    submitIntentAwaitingRef: () => null,
    isSocketOpen: () => false,
    // `sessionRegistry.ts`'s `has_error`/marker migration
    // (`onSummaryV2`) subscribes unconditionally in `bind()` — a no-op
    // unsubscribe keeps the broad suite on its pre-migration
    // `session_error_changed`/`session_marker_changed` eventBus behavior
    // (this stub never calls back, so `onSummaryV2` never fires; tests
    // exercising it mock this module themselves, same escape hatch as
    // `useFoldersV2`/`useTagsV2` above).
    subscribeAllSummaries: () => () => {},
  },
  useSessionSummaryV2: () => undefined,
  useProjectsV2: () => [],
  useFoldersV2: () => mockFoldersV2State,
  useTagsV2: () => mockTagsV2State,
  useModelFacetV2: () => mockModelFacetState,
  useSessionSocketOpenV2: () => false,
  /** Legacy `SessionFolder[]`/`SessionTag[]` shape in (same objects a test
   * already builds for its `/api/session-organization` fetch stub) —
   * mapped here to v2 wire shape so call sites don't need to know the
   * rename (`id`->`folder_ref`/`tag_ref`, `project_id`->`project_ref`,
   * `parent_folder_id`->`parent_folder_ref`). */
  __setMockSessionOrganizationV2: (snapshot: {
    folders?: Array<{ id: string; project_id: string; parent_folder_id?: string | null; name: string; order: number }>;
    tags?: Array<{ id: string; project_id?: string | null; name: string; color?: string | null }>;
    models?: string[];
  }) => {
    mockFoldersV2State = (snapshot.folders ?? []).map((f) => ({
      folder_ref: f.id,
      project_ref: f.project_id,
      parent_folder_ref: f.parent_folder_id ?? null,
      name: f.name,
      order: f.order,
    }));
    mockTagsV2State = (snapshot.tags ?? []).map((t) => ({
      tag_ref: t.id,
      project_ref: t.project_id ?? null,
      name: t.name,
      color: t.color ?? "",
    }));
    mockModelFacetState = snapshot.models ?? [];
  },
}));

// `lib/interactionResolveSocket.ts` (ADR 0006 §5's UserInteraction resolve
// plane) — same class of always-on, module-level `/ws/v2/surface` singleton
// as `sessionSurfaceRegistry` above, opened lazily on first mount
// (`UserInteractionCards.tsx`'s card cores + `Chat.tsx`'s `ToolApprovalCard`/
// worker-approval callbacks all call `useInteractionResolveSocket()`
// unconditionally). Unlike `sessionSurfaceRegistry`'s AWAITED intent
// (which hangs a promise forever with no ack simulation), nothing here
// blocks on the connection — but happy-dom's real `WebSocket` can still
// reach `readyState === OPEN` within a test's async window with no real
// backend listening, non-deterministically flipping `submitResolveInteraction`
// from its intended "not open -> REST fallback" path onto a code path that
// calls `socket.submit()` against a socket nothing in the test ever primes
// an ack for — the resolve promise then never settles, and the REST
// fallback assertion the broad suite already relies on (dozens of pre-
// existing approval/user-input tests) silently stops firing. Stubbed here,
// same rationale/pattern as `sessionSurfaceRegistry`: the broad suite
// exercises the pre-migration REST-fallback behavior unchanged
// (`submitResolveInteraction` returning `null` is its own documented
// "not open" contract). A dedicated test file that wants the REAL v2
// resolve-intent behavior overrides this via its own `vi.mock`/`vi.doMock`.
// ADR 0011 (Package C, system & host surface): `lib/systemFeedRegistry.ts`
// is the same class of always-on, module-level `/ws/v2/surface` singleton
// as `sessionSurfaceRegistry`/`interactionResolveSocket` above — every
// consumer (`HarnessProfileSelector.tsx`,
// `HarnessSettingsEditor.tsx`, `ExtensionUiHooks.tsx`, `ExtensionSlots.tsx`,
// `MarketplaceBridgeCenter.tsx`, `SchedulesPage.tsx`,
// `SessionBackgroundStrip.tsx`, `SettingsPage.tsx`'s extension-catalog
// section, `useMachines.ts`) subscribes unconditionally in an effect/at
// module load, and several of them have DEDICATED test files that mount
// the component directly (not via `renderApp()`'s WS-mocked harness) —
// without this stub, happy-dom's real `WebSocket` throws synchronously on
// construction in that context (`surfaceWsUrl()` resolves to a relative
// URL happy-dom rejects outside the full app harness), crashing the whole
// render. Same rationale/pattern as `sessionSurfaceRegistry`/
// `interactionResolveSocket`: the broad suite exercises the pre-migration
// legacy behavior unchanged (`submitSystemIntent` returning `null` is its
// own documented "not open" contract, `subscribeSystemFrames` never firing
// simply means no live v2 update — every consumer's legacy eventBus path
// still drives its rendered state). A dedicated test file that wants the
// REAL v2 system-plane behavior overrides this via `vi.unmock`, same
// escape hatch `tests/sessionSurfaceRegistry.test.ts`/
// `tests/interactionResolveSocket.test.tsx` already use.
vi.mock("../src/lib/systemFeedRegistry", () => ({
  SYSTEM_FEED_NAMES: [
    "extension_notices", "extension_config", "harness_profiles", "extension_ui",
    "extension_catalog", "marketplace_bridge", "marketplace_intents", "schedules",
    "host_startup_tasks", "installation_capabilities", "machines", "node_registrations",
  ],
  subscribeSystemFrames: () => () => {},
  subscribeSystemSocketConnection: () => () => {},
  isSystemSocketOpen: () => false,
  submitSystemIntent: () => null,
  // `waitForSystemFrame`/`submitSystemIntentAwaitingUpsert`
  // (`HarnessSettingsEditor.tsx`'s create flow,
  // `useMachines.ts`'s credential-sync-result collection) — same not-
  // open/never-fires contract as `submitSystemIntent`/`subscribeSystemFrames`
  // above.
  waitForSystemFrame: () => new Promise(() => {}),
  submitSystemIntentAwaitingUpsert: () => null,
}));

vi.mock("../src/lib/interactionResolveSocket", () => ({
  submitResolveInteraction: () => null,
  // `tryResolveInteraction` is `submitResolveInteraction`'s own thin
  // "returns false when not open" wrapper (see that module) — same
  // not-open contract, stubbed here for the identical reason.
  tryResolveInteraction: async () => false,
  isInteractionResolveSocketOpen: () => false,
  acquireInteractionResolveSocket: () => () => {},
  useInteractionResolveSocket: () => ({ open: false }),
  USER_INPUT_REF_PREFIX: "user_input:",
  DELEGATE_CHOICE_REF_PREFIX: "delegate_choice:",
}));

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
