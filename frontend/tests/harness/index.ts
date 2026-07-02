import { act, render, type RenderResult } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import React from "react";
import { afterEach } from "vitest";
import App from "../../src/App";
import type { Session, WSEvent } from "../../src/types";
import { MockBackend, type BackendState } from "./mockBackend";
import { MockWebSocketController, type OutboundFrame } from "./mockWebSocket";
import { extractView, type AppView } from "./view";

export interface RenderAppOptions {
  seed?: Partial<BackendState>;
}

const activeHarnessCleanups = new Set<() => void>();

afterEach(() => {
  for (const dispose of Array.from(activeHarnessCleanups)) {
    dispose();
  }
});

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
  /** Push a single WS event into the app. */
  emit(event: WSEvent): void;
  /** Push many WS events in sequence. */
  emitMany(events: WSEvent[]): void;
  /** Type into the input box and click Send. */
  typeAndSend(text: string): Promise<void>;
  /** Click a session row to make it the current one. */
  selectSession(sessionId: string): Promise<void>;
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

export async function renderApp(options: RenderAppOptions = {}): Promise<Harness> {
  const backend = new MockBackend();
  if (options.seed) backend.seed(options.seed);
  backend.install();

  const wsController = new MockWebSocketController();
  wsController.install();
  let result: RenderResult | null = null;
  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    result?.unmount();
    wsController.uninstall();
    backend.uninstall();
    activeHarnessCleanups.delete(dispose);
  };
  activeHarnessCleanups.add(dispose);

  // user-event v14 needs to be set up before render; configure it to
  // skip pointer hover & autoAdvanceTimers so happy-dom tolerates it.
  const user: UserEvent = userEvent.setup({
    delay: null,
    pointerEventsCheck: 0,
  });

  try {
    result = render(React.createElement(App));
    // Let the initial fetches + WS open + first effects flush.
    await flushAll();
  } catch (error) {
    dispose();
    throw error;
  }
  const rendered = result;

  const harness: Harness = {
    toJSON: () => extractView(rendered.container as HTMLElement),
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
    emit: (event) => wsController.emit(event),
    emitMany: (events) => wsController.emitMany(events),
    typeAndSend: async (text: string) => {
      const ta = rendered.container.querySelector(
        '[data-testid="input-textarea"]',
      ) as HTMLTextAreaElement | null;
      if (!ta) throw new Error("Harness: input textarea not present");
      await user.click(ta);
      await user.type(ta, text);
      const sendBtn = rendered.container.querySelector(
        '[data-testid="send-btn"]',
      ) as HTMLButtonElement | null;
      if (!sendBtn) throw new Error("Harness: send button not present");
      await user.click(sendBtn);
      await flushAll();
    },
    selectSession: async (sessionId: string) => {
      const row = rendered.container.querySelector(
        `[data-testid="session-item"][data-session-id="${cssEscape(sessionId)}"]`,
      ) as HTMLElement | null;
      if (!row) throw new Error(`Harness: session ${sessionId} not in list`);
      await user.click(row);
      await flushAll();
    },
    approveWorker: async (delegationId: string) => {
      const card = findApprovalCard(rendered.container as HTMLElement, delegationId);
      const btn = card.querySelector("button.approve") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: approve button missing");
      await user.click(btn);
      await flushAll();
    },
    denyWorker: async (delegationId: string) => {
      const card = findApprovalCard(rendered.container as HTMLElement, delegationId);
      const btn = card.querySelector("button.deny") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: deny button missing");
      await user.click(btn);
      await flushAll();
    },
    approveCredential: async (consentId: string, secret: string = "") => {
      const card = findCredentialCard(rendered.container as HTMLElement, consentId);
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
      const card = findCredentialCard(rendered.container as HTMLElement, consentId);
      const btn = card.querySelector("button.deny") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: deny button missing");
      await user.click(btn);
      await flushAll();
    },
    backend,
    dropConnection: () => wsController.closeCurrent(),
    reopenConnection: () => wsController.reopenCurrent(),
    flush: flushAll,
    unmount: () => {
      dispose();
    },
    raw: rendered,
    clickByText: async (text: string | RegExp) => {
      const re = text instanceof RegExp ? text : new RegExp(`^\\s*${escapeRegex(text)}\\s*$`);
      const buttons = Array.from(
        rendered.container.querySelectorAll<HTMLButtonElement>("button"),
      );
      const match = buttons.find((b) => re.test(b.textContent ?? ""));
      if (!match) throw new Error(`Harness: no button matching ${text}`);
      await user.click(match);
      await flushAll();
    },
    deleteSession: async (sessionId: string) => {
      const row = rendered.container.querySelector(
        `[data-testid="session-item"][data-session-id="${cssEscape(sessionId)}"]`,
      );
      if (!row) throw new Error(`Harness: session ${sessionId} not in list`);
      const del = row.querySelector(".session-item-delete") as HTMLButtonElement | null;
      if (!del) throw new Error("Harness: delete button missing");
      await user.click(del);
      await flushAll();
      // Confirm the deletion in the modal.
      const modal = rendered.container.querySelector(".modal-overlay");
      if (modal) {
        const confirmBtn = modal.querySelector(".modal-footer button:last-child") as HTMLButtonElement | null;
        if (confirmBtn) {
          await user.click(confirmBtn);
          await flushAll();
        }
      }
    },
    renameSession: async (sessionId: string, newName: string) => {
      const row = rendered.container.querySelector(
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
      const btn = rendered.container.querySelector(".stop-btn") as HTMLButtonElement | null;
      if (!btn) throw new Error("Harness: stop button not visible");
      await user.click(btn);
      await flushAll();
    },
    $: (selector: string) =>
      rendered.container.querySelector<HTMLElement>(selector),
    $$: (selector: string) =>
      Array.from(rendered.container.querySelectorAll<HTMLElement>(selector)),
    click: async (selector: string) => {
      const el = rendered.container.querySelector<HTMLElement>(selector);
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
