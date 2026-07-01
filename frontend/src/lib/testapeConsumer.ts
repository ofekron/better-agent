import type { Session } from "src/types";

type TestApeClient = {
  sendState(key: string, value: unknown): void;
};

type ChatPanelMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};

type ChatPanelRegion = {
  kind: "linear" | "fork_shared" | "fork_pane";
  session_id: string | null;
  focused?: boolean;
  messages: ChatPanelMessage[];
};

type ChatPanelTree = {
  visible: boolean;
  session_id: string | null;
  title: string | null;
  regions: ChatPanelRegion[];
};

type UpdateInput = {
  authStatus: string;
  connected: boolean;
  viewport: string;
  sessions: readonly Session[];
  currentSession: Session | null;
  openSessionIds: readonly string[];
  pendingMessageCount: number;
  queuedPromptCount: number;
  rightPanelOpen: boolean;
  rightPanelTab: string | null;
};

declare global {
  interface Window {
    testape?: TestApeClient;
    __betterAgentTestApe?: {
      extractVisibleChatPanelTree(): ChatPanelTree;
      publishVisibleChatPanelTree(): ChatPanelTree | null;
    };
  }
}

let latestSessionId: string | null = null;

function client(): TestApeClient | null {
  return typeof window !== "undefined" && window.testape ? window.testape : null;
}

function sessionPayload(session: Session | null): Record<string, unknown> | null {
  if (!session) return null;
  return {
    id: session.id,
    name: session.name || "",
    cwd: session.cwd || "",
    provider_id: session.provider_id || "",
    model: session.model || "",
    orchestration_mode: session.orchestration_mode || "",
    message_count: session.messages?.length ?? 0,
  };
}

function sendState(testape: TestApeClient, key: string, value: unknown): void {
  testape.sendState(`better_agent_${key}`, value);
}

function tidyText(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function sessionIdFromLocation(): string | null {
  if (typeof window === "undefined") return null;
  const match = window.location.pathname.match(/^\/s\/([^/]+)(?:\/.*)?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function elementIsVisible(el: HTMLElement, viewport: DOMRect): boolean {
  const style = window.getComputedStyle(el);
  if (style.display === "none" || style.visibility === "hidden") return false;
  if (el.closest('[aria-hidden="true"], [hidden]')) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0 && viewport.width > 0 && viewport.height > 0) {
    return false;
  }
  if (viewport.width === 0 || viewport.height === 0) return true;
  return (
    rect.bottom >= viewport.top &&
    rect.top <= viewport.bottom &&
    rect.right >= viewport.left &&
    rect.left <= viewport.right
  );
}

function readMessage(el: HTMLElement): ChatPanelMessage | null {
  const role =
    el.dataset.testid === "user-message" || el.classList.contains("user-message")
      ? "user"
      : el.dataset.testid === "assistant-message"
        ? "assistant"
        : null;
  const id = el.getAttribute("data-message-id");
  if (!role || !id) return null;
  const textEl =
    role === "user"
      ? el.querySelector<HTMLElement>(".message-box-body, .message-content")
      : el.querySelector<HTMLElement>(".message-content");
  return { id, role, text: tidyText(textEl?.textContent ?? el.textContent) };
}

function readRegion(
  root: HTMLElement,
  kind: ChatPanelRegion["kind"],
  sessionId: string | null,
  outerViewport: DOMRect,
  focused?: boolean,
): ChatPanelRegion {
  const viewport = effectiveRegionViewport(root, outerViewport);
  if (!viewport) return { kind, session_id: sessionId, focused, messages: [] };
  const messages = Array.from(
    root.querySelectorAll<HTMLElement>(
      [
        '[data-testid="user-message"]',
        '[data-testid="assistant-message"]',
        ".user-message[data-message-id]",
      ].join(", "),
    ),
  )
    .filter((el) => elementIsVisible(el, viewport))
    .map(readMessage)
    .filter((m): m is ChatPanelMessage => m !== null);
  return { kind, session_id: sessionId, focused, messages };
}

function effectiveRegionViewport(root: HTMLElement, outerViewport: DOMRect): DOMRect | null {
  const regionRect = regionViewport(root);
  let viewport = regionRect;
  if (hasArea(outerViewport) && hasArea(regionRect)) {
    viewport = intersectRects(outerViewport, regionRect);
    if (!hasArea(viewport)) return null;
  }
  const forkGrid = root.closest<HTMLElement>('[data-testid="fork-grid"]');
  if (forkGrid) {
    const gridRect = forkGrid.getBoundingClientRect();
    if (hasArea(gridRect) && hasArea(viewport)) {
      viewport = intersectRects(viewport, gridRect);
      if (!hasArea(viewport)) return null;
    }
  }
  return viewport;
}

function hasArea(rect: DOMRect): boolean {
  return rect.width > 0 && rect.height > 0;
}

function regionViewport(root: HTMLElement): DOMRect {
  return (
    root.querySelector<HTMLElement>(".fork-pane-messages")?.getBoundingClientRect() ??
    root.getBoundingClientRect()
  );
}

function intersectRects(a: DOMRect, b: DOMRect): DOMRect {
  const left = Math.max(a.left, b.left);
  const right = Math.min(a.right, b.right);
  const top = Math.max(a.top, b.top);
  const bottom = Math.min(a.bottom, b.bottom);
  const width = Math.max(0, right - left);
  const height = Math.max(0, bottom - top);
  return {
    left,
    right,
    top,
    bottom,
    width,
    height,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

export function extractVisibleChatPanelTree(): ChatPanelTree {
  const chat = document.querySelector<HTMLElement>('[data-testid="chat-messages"]');
  const title = tidyText(document.querySelector<HTMLElement>(".chat-toolbar-title")?.textContent);
  const currentSessionId = sessionIdFromLocation() ?? latestSessionId;
  if (!chat) {
    return { visible: false, session_id: currentSessionId, title: title || null, regions: [] };
  }
  const viewport = chat.getBoundingClientRect();
  const regions: ChatPanelRegion[] = [];
  const forkGrid = chat.querySelector<HTMLElement>('[data-testid="fork-grid"]');
  if (forkGrid) {
    const shared = chat.querySelector<HTMLElement>('[data-testid="fork-shared"]');
    if (shared && elementIsVisible(shared, viewport)) {
      regions.push(readRegion(shared, "fork_shared", currentSessionId, viewport));
    }
    for (const pane of Array.from(chat.querySelectorAll<HTMLElement>('[data-testid="fork-pane"]'))) {
      if (!elementIsVisible(pane, viewport)) continue;
      regions.push(
        readRegion(
          pane,
          "fork_pane",
          pane.getAttribute("data-session-id"),
          viewport,
          pane.classList.contains("fork-pane-focused"),
        ),
      );
    }
  } else {
    regions.push(readRegion(chat, "linear", currentSessionId, viewport));
  }
  return {
    visible: true,
    session_id: currentSessionId,
    title: title || null,
    regions,
  };
}

export function publishBetterAgentVisibleChatPanelTree(): ChatPanelTree | null {
  const testape = client();
  if (!testape) return null;
  const tree = extractVisibleChatPanelTree();
  sendState(testape, "visible_chat_panel_tree", tree);
  return tree;
}

function installBetterAgentTestApeHooks(): void {
  if (typeof window === "undefined") return;
  window.__betterAgentTestApe = {
    extractVisibleChatPanelTree,
    publishVisibleChatPanelTree: publishBetterAgentVisibleChatPanelTree,
  };
}

installBetterAgentTestApeHooks();

export function publishBetterAgentTestApeState(input: UpdateInput): void {
  const testape = client();
  latestSessionId = input.currentSession?.id ?? null;
  installBetterAgentTestApeHooks();
  if (!testape) return;
  const currentSession = sessionPayload(input.currentSession);
  const openFilePanels = input.currentSession?.open_file_panels ?? [];

  sendState(testape, "auth_status", input.authStatus);
  sendState(testape, "backend_connected", input.connected);
  sendState(testape, "viewport", input.viewport);
  sendState(testape, "session_count", input.sessions.length);
  sendState(testape, "current_session", currentSession);
  sendState(testape, "current_session_id", input.currentSession?.id ?? null);
  sendState(testape, "open_session_ids", [...input.openSessionIds]);
  sendState(testape, "pending_message_count", input.pendingMessageCount);
  sendState(testape, "queued_prompt_count", input.queuedPromptCount);
  sendState(testape, "right_panel", {
    open: input.rightPanelOpen,
    tab: input.rightPanelTab,
  });
  sendState(testape, "native_file_panel_count", openFilePanels.length);
  sendState(testape, "native_file_panel_paths", openFilePanels.map((panel) => panel.path));
}
