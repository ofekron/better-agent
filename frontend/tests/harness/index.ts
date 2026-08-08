import { act, render, type RenderResult } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import React from "react";
import { afterEach } from "vitest";
import App from "../../src/App";
import { loadBuiltinExtensionIds } from "../../src/extensionIds";
import type { Session, WSEvent } from "../../src/types";
import type { ChatFrame } from "../../src/adapter/wire";
import { MockBackend, type BackendState } from "./mockBackend";
import { MockWebSocketController, type OutboundFrame } from "./mockWebSocket";
import { extractView, type AppView } from "./view";

export interface RenderAppOptions {
  seed?: Partial<BackendState>;
  configureBackend?: (backend: MockBackend) => void;
  autoOpenWebSocket?: boolean;
}

export interface Harness {
  /** Structured JSON snapshot of what's currently visible. */
  toJSON(): AppView;
  /** Outbound frames the app sent through the WebSocket. */
  readonly outbound: OutboundFrame[];
  /** REST calls captured by the mock backend. */
  readonly restCalls: {
    method: string;
    path: string;
    credentials?: RequestCredentials;
    body?: unknown;
  }[];
  /** Id the client minted for a create it POSTed (negative index counts
   * back from the newest). Session ids are minted client-side so a create
   * that fails mid-flight replays under the same id — tests must read the
   * id back instead of assuming how the backend numbered it. */
  createdSessionId(index?: number): string;
  /** Push a single WS event into the app. */
  emit(event: WSEvent): void;
  /** Push many WS events in sequence. */
  emitMany(events: WSEvent[]): void;
  /** Pre-populate a session's native Contract-Node surface mock state
   *  (served by GET /api/v2/surface/sessions/:id/{snapshot,nodes/*
   *  /children,older}) — see MockBackend.seedSurface. Call before
   *  `selectSession`/mounting ChatSurfaceView so its initial fetchSnapshot
   *  sees the seeded content instead of the always-empty default. */
  seedSurface(sessionId: string, partial: Parameters<MockBackend["seedSurface"]>[1]): void;
  /** Push one live `/ws/v2/surface` ChatFrame into the app AND project it
   *  onto the mock's surface state (mirrors `emit`'s applyWsEvent
   *  pairing) — targets the native socket specifically via
   *  MockWebSocketController.emitTo, since a legacy `/ws/chat` socket may
   *  also be open concurrently in the same test. */
  emitSurface(sessionId: string, frame: ChatFrame): void;
  /** Emit `session_monitoring_changed` — the backend-owned source of truth
   * for the Running dimension (docs/session-states.md). In production the
   * monitoring loop announces active/stopped alongside `run_state`, so tests
   * simulating an in-flight turn pair the two. */
  setMonitoring(
    sessionId: string,
    state: "active" | "stopped" | "waiting_on_background",
  ): void;
  /** Type into the input box and click Send. */
  typeAndSend(text: string): Promise<void>;
  /** Click a session row to make it the current one. */
  selectSession(sessionId: string): Promise<void>;
  /** Expand a collapsed turn group via its header — the same click a user
   * makes. Completed turns default to collapsed (docs/chat-panel.md), so
   * tests asserting on a turn's assistant/response DOM expand it first.
   * No-op when the turn is already expanded. */
  expandTurn(initiatorMessageId: string): Promise<void>;
  /** Approve a pending fresh-worker creation card. */
  approveWorker(delegationId: string): Promise<void>;
  /** Deny a pending fresh-worker creation card. */
  denyWorker(delegationId: string): Promise<void>;
  /** Enter the secret into a credential consent card and click Approve. */
  approveCredential(consentId: string, secret?: string): Promise<void>;
  /** Deny a credential consent card. */
  denyCredential(consentId: string): Promise<void>;
  /** Direct backend state — useful for in-test seeding/inspection. */
  readonly backend: MockBackend;
  /** Drop the WS connection (exercises reconnect path). */
  dropConnection(): void;
  /** Re-open the current WS immediately. */
  reopenConnection(): void;
  /** Force a microtask + timer flush so React effects settle. */
  flush(): Promise<void>;
  /** Poll `predicate` (flushing between attempts) until it returns true
   * or `timeoutMs` elapses, then throws. Chat.tsx throttles turn-group
   * re-renders to one commit per 140ms while the session is running
   * (`useThrottledValue`) — a single `flush()`'s zero-delay tick can
   * land before that trailing timer fires, so any assertion on DOM
   * state produced by a live WS event on a running session must poll
   * past the real 140ms window instead of flushing once. */
  waitFor(predicate: () => boolean, timeoutMs?: number): Promise<void>;
  /** Tear down without cleanup() being called by setup.ts afterEach. */
  unmount(): void;
  readonly raw: RenderResult;
  /** Click a button by visible text (first match). Throws if not found. */
  clickByText(text: string | RegExp): Promise<void>;
  /** Click a session row's delete (×) icon by session id. */
  deleteSession(sessionId: string): Promise<void>;
  /** Click a session row's rename (✎) icon by session id, type new name, press Enter. */
  renameSession(sessionId: string, newName: string): Promise<void>;
  /** Click the streaming bubble's Stop button. */
  clickStop(): Promise<void>;
  /** Direct query helper. */
  $(selector: string): HTMLElement | null;
  $$(selector: string): HTMLElement[];
  /** Click an element by selector. */
  click(selector: string): Promise<void>;
}

const activeHarnessTeardowns = new Set<() => void>();

afterEach(() => {
  for (const teardown of [...activeHarnessTeardowns].reverse()) teardown();
});

export async function renderApp(options: RenderAppOptions = {}): Promise<Harness> {
  const backend = new MockBackend();
  if (options.seed) backend.seed(options.seed);
  options.configureBackend?.(backend);
  backend.install();

  const wsController = new MockWebSocketController(
    options.autoOpenWebSocket ?? true,
  );
  wsController.install();

  // user-event v14 needs to be set up before render; configure it to
  // skip pointer hover & autoAdvanceTimers so happy-dom tolerates it.
  const user: UserEvent = userEvent.setup({
    delay: null,
    pointerEventsCheck: 0,
  });

  // The real bootstrap (main.tsx) gates first paint on the builtin-ids
  // load; mirror it so extBackendBase() resolves ids before App's mount
  // fetches fire against the mock's extension-backend proxy routes.
  await loadBuiltinExtensionIds();

  const result = render(React.createElement(App));
  let active = true;
  const teardown = () => {
    if (!active) return;
    active = false;
    activeHarnessTeardowns.delete(teardown);
    result.unmount();
    wsController.uninstall();
    backend.uninstall();
  };
  activeHarnessTeardowns.add(teardown);
  // Let the initial fetches + WS open + first effects flush.
  await flushAll();

  const harness: Harness = {
    toJSON: () => extractView(result.container as HTMLElement),
    get outbound() {
      return wsController.outbound;
    },
    get restCalls() {
      return backend.calls.map((c) => ({
        method: c.method,
        path: c.path,
        credentials: c.credentials,
        body: c.body,
      }));
    },
    createdSessionId: (index = -1) => {
      const creates = backend.calls.filter(
        (c) => c.method === "POST" && c.path === "/api/sessions",
      );
      const call = index < 0 ? creates[creates.length + index] : creates[index];
      const id = (call?.body as { client_session_id?: string } | undefined)
        ?.client_session_id;
      if (!id) {
        throw new Error(
          `no client_session_id on create #${index} of ${creates.length}`,
        );
      }
      return id;
    },
    emit: (event) => {
      backend.applyWsEvent(event);
      wsController.emit(event);
    },
    emitMany: (events) => {
      for (const event of events) backend.applyWsEvent(event);
      wsController.emitMany(events);
    },
    seedSurface: (sessionId, partial) => backend.seedSurface(sessionId, partial),
    emitSurface: (sessionId, frame) => {
      backend.applySurfaceFrame(sessionId, frame);
      wsController.emitTo("/ws/v2/surface", frame);
    },
    setMonitoring: (sessionId, state) => {
      const event = {
        type: "session_monitoring_changed",
        data: { session_id: sessionId, monitoring_state: state },
      } as WSEvent;
      backend.applyWsEvent(event);
      wsController.emit(event);
    },
    typeAndSend: async (text: string) => {
      const ta = result.container.querySelector(
        '[data-testid="input-textarea"]',
      ) as HTMLTextAreaElement | null;
      if (!ta) throw new Error("Harness: input textarea not present");
      await user.click(ta);
      await user.type(ta, text);
      const sendBtn = result.container.querySelector(
        '[data-testid="send-btn"]',
      ) as HTMLButtonElement | null;
      if (!sendBtn) throw new Error("Harness: send button not present");
      await user.click(sendBtn);
      await flushAll();
    },
    selectSession: async (sessionId: string) => {
      const row = result.container.querySelector(
        `[data-testid="session-item"][data-session-id="${cssEscape(sessionId)}"]`,
      ) as HTMLElement | null;
      if (!row) throw new Error(`Harness: session ${sessionId} not in list`);
      await user.click(row);
      await flushAll();
    },
    expandTurn: async (initiatorMessageId: string) => {
      const box = result.container.querySelector(
        `[data-message-id="${cssEscape(initiatorMessageId)}"]`,
      );
      const header = box?.querySelector(
        '.message-box-header-main[aria-expanded="false"]',
      ) as HTMLButtonElement | null;
      if (!header) return;
      await user.click(header);
      await flushAll();
    },
    approveWorker: async (delegationId: string) => {
      const card = findApprovalCard(result.container as HTMLElement, delegationId);
      const btn = card.querySelector("button.approve") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: approve button missing");
      await user.click(btn);
      await flushAll();
    },
    denyWorker: async (delegationId: string) => {
      const card = findApprovalCard(result.container as HTMLElement, delegationId);
      const btn = card.querySelector("button.deny") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: deny button missing");
      await user.click(btn);
      await flushAll();
    },
    approveCredential: async (consentId: string, secret: string = "") => {
      const card = findCredentialCard(result.container as HTMLElement, consentId);
      const input = card.querySelector(
        '[data-testid="credential-secret-input"]',
      ) as HTMLInputElement | null;
      if (input) {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype,
          "value",
        )?.set;
        setter?.call(input, secret);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        await flushAll();
      }
      const btn = card.querySelector("button.approve") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: approve button missing");
      await user.click(btn);
      await flushAll();
    },
    denyCredential: async (consentId: string) => {
      const card = findCredentialCard(result.container as HTMLElement, consentId);
      const btn = card.querySelector("button.deny") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: deny button missing");
      await user.click(btn);
      await flushAll();
    },
    backend,
    dropConnection: () => wsController.closeCurrent(),
    reopenConnection: () => wsController.reopenCurrent(),
    flush: flushAll,
    waitFor: async (predicate: () => boolean, timeoutMs = 300) => {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        await flushAll();
        if (predicate()) return;
        if (Date.now() >= deadline) {
          throw new Error(`Harness: waitFor predicate did not hold within ${timeoutMs}ms`);
        }
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    },
    unmount: teardown,
    raw: result,
    clickByText: async (text: string | RegExp) => {
      const re = text instanceof RegExp ? text : new RegExp(`^\\s*${escapeRegex(text)}\\s*$`);
      const buttons = Array.from(
        result.container.querySelectorAll<HTMLButtonElement>("button"),
      );
      const match = buttons.find((b) => re.test(b.textContent ?? ""));
      if (!match) throw new Error(`Harness: no button matching ${text}`);
      await user.click(match);
      await flushAll();
    },
    deleteSession: async (sessionId: string) => {
      const row = result.container.querySelector(
        `[data-testid="session-item"][data-session-id="${cssEscape(sessionId)}"]`,
      );
      if (!row) throw new Error(`Harness: session ${sessionId} not in list`);
      const del = row.querySelector(".session-item-delete") as HTMLButtonElement | null;
      if (!del) throw new Error("Harness: delete button missing");
      await user.click(del);
      await flushAll();
      // Confirm the deletion in the modal.
      const modal = result.container.querySelector(".modal-overlay");
      if (modal) {
        const confirmBtn = modal.querySelector(".modal-footer button:last-child") as HTMLButtonElement | null;
        if (confirmBtn) {
          await user.click(confirmBtn);
          await flushAll();
        }
      }
    },
    renameSession: async (sessionId: string, newName: string) => {
      const row = result.container.querySelector(
        `[data-testid="session-item"][data-session-id="${cssEscape(sessionId)}"]`,
      );
      if (!row) throw new Error(`Harness: session ${sessionId} not in list`);
      const rename = row.querySelector(".session-item-rename") as HTMLButtonElement | null;
      if (!rename) throw new Error("Harness: rename button missing");
      await user.click(rename);
      await flushAll();
      const input = row.querySelector(".session-rename-input") as HTMLInputElement | null;
      if (!input) throw new Error("Harness: rename input did not appear");
      await user.clear(input);
      await user.type(input, newName);
      await user.keyboard("{Enter}");
      await flushAll();
    },
    clickStop: async () => {
      const btn = result.container.querySelector(".stop-btn") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: stop button not visible");
      await user.click(btn);
      await flushAll();
    },
    $: (selector: string) =>
      result.container.querySelector<HTMLElement>(selector),
    $$: (selector: string) =>
      Array.from(result.container.querySelectorAll<HTMLElement>(selector)),
    click: async (selector: string) => {
      const el = result.container.querySelector<HTMLElement>(selector);
      if (!el) throw new Error(`Harness: no element matching ${selector}`);
      await user.click(el);
      await flushAll();
    },
  };

  return harness;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function flushAll(): Promise<void> {
  // Drain microtasks (fetch resolves, queueMicrotask in MockWebSocket)
  // and let React's commit phase finish via act().
  await act(async () => {
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    await Promise.resolve();
  });
}

function findApprovalCard(container: HTMLElement, delegationId: string): HTMLElement {
  const card = container.querySelector(
    `[data-testid="worker-approval-card"][data-delegation-id="${cssEscape(delegationId)}"]`,
  ) as HTMLElement | null;
  if (!card) throw new Error(`Harness: approval card ${delegationId} not present`);
  return card;
}

function findCredentialCard(container: HTMLElement, consentId: string): HTMLElement {
  const card = container.querySelector(
    `[data-testid="credential-consent-card"][data-consent-id="${cssEscape(consentId)}"]`,
  ) as HTMLElement | null;
  if (!card) throw new Error(`Harness: credential card ${consentId} not present`);
  return card;
}

function cssEscape(s: string): string {
  // happy-dom's CSS.escape may be missing; fall back to a tight allowlist.
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(s);
  }
  return s.replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

export type { AppView } from "./view";
export type { Session };
