/** DOM → structured JSON snapshot of the rendered app. Tests assert
 *  against this instead of querying nodes directly, so the assertion
 *  surface is decoupled from per-component DOM details. */

export interface MessageView {
  id: string;
  role: "user" | "assistant";
  text: string;
  status?: string;
}

export interface RunBadgeView {
  kind: "manager" | "native" | "worker";
  /** Visible label, e.g. "manager running" or "worker · Researcher running". */
  label: string;
}

export interface SessionItemView {
  id: string;
  name: string;
  active: boolean;
}

export interface ApprovalView {
  delegationId: string;
  text: string;
}

export interface CredentialView {
  consentId: string;
  text: string;
  sinkText: string;
  risk: string;
  mismatch: boolean;
  egress: boolean;
}

export interface AppView {
  sidebar: {
    sessions: SessionItemView[];
    workersPanelVisible: boolean;
    workerCount: number | null;
  };
  chat: {
    visible: boolean;
    title: string | null;
    messages: MessageView[];
    /** Backend-owned run-state badges currently rendered. The synthetic
     *  "streaming bubble" no longer exists — runs drive the
     *  "running" affordance instead. */
    runs: RunBadgeView[];
    /** True when the focused chat session is running. */
    running: boolean;
    /** True when a Stop button is rendered (driven by runs + onStop). */
    stopButtonVisible: boolean;
    /** True while Stop is acknowledging over WS or REST. */
    stopButtonDisabled: boolean;
    /** True while Stop shows its in-progress state. */
    stopButtonStopping: boolean;
    approvals: ApprovalView[];
    credentials: CredentialView[];
  };
  input: {
    present: boolean;
    text: string;
    disabled: boolean;
    sendDisabled: boolean;
  };
}

function tidyText(s: string | null | undefined): string {
  return (s ?? "").replace(/\s+/g, " ").trim();
}

function readSessions(container: HTMLElement): SessionItemView[] {
  const items = container.querySelectorAll<HTMLElement>(
    '[data-testid="session-item"]',
  );
  return Array.from(items).map((el) => {
    const nameEl = el.querySelector<HTMLElement>(".session-item-name");
    return {
      id: el.dataset.sessionId ?? "",
      name: tidyText(nameEl?.childNodes[nameEl.childNodes.length - 1]?.textContent ?? nameEl?.textContent ?? ""),
      active: el.dataset.active === "true",
    };
  });
}

function readMessage(el: HTMLElement, role: "user" | "assistant"): MessageView {
  const id = el.getAttribute("data-message-id") ?? "";
  let textNode: HTMLElement | null = null;
  if (role === "user") {
    textNode = el.querySelector<HTMLElement>(".message-box-body");
  } else {
    textNode = el.querySelector<HTMLElement>(".message-content") ?? el;
  }
  const text = tidyText(textNode?.textContent ?? "");
  const status = el.getAttribute("data-status") || undefined;
  return {
    id,
    role,
    text,
    status: status || undefined,
  };
}

function readMessages(container: HTMLElement): MessageView[] {
  const out: MessageView[] = [];
  const chatMessages = container.querySelector<HTMLElement>('[data-testid="chat-messages"]');
  if (!chatMessages) return out;
  // Walk in document order so user/assistant pairs interleave correctly.
  const all = chatMessages.querySelectorAll<HTMLElement>(
    '[data-testid="user-message"], [data-testid="assistant-message"]',
  );
  for (const el of Array.from(all)) {
    const role = el.dataset.testid === "user-message" ? "user" : "assistant";
    out.push(readMessage(el, role));
  }
  return out;
}

function readRuns(container: HTMLElement): RunBadgeView[] {
  const badges = container.querySelectorAll<HTMLElement>(".run-badge");
  return Array.from(badges).map((el) => ({
    kind: (el.getAttribute("data-kind") as RunBadgeView["kind"]) ?? "manager",
    label: tidyText(el.querySelector(".run-badge-label")?.textContent ?? ""),
  }));
}

function readApprovals(container: HTMLElement): ApprovalView[] {
  const cards = container.querySelectorAll<HTMLElement>(
    '[data-testid="worker-approval-card"]',
  );
  return Array.from(cards).map((el) => ({
    delegationId: el.dataset.delegationId ?? "",
    text: tidyText(el.textContent ?? ""),
  }));
}

function readCredentials(container: HTMLElement): CredentialView[] {
  const cards = container.querySelectorAll<HTMLElement>(
    '[data-testid="credential-consent-card"]',
  );
  return Array.from(cards).map((el) => ({
    consentId: el.dataset.consentId ?? "",
    text: tidyText(el.textContent ?? ""),
    sinkText: tidyText(
      el.querySelector('[data-testid="credential-sink"]')?.textContent ?? "",
    ),
    risk: tidyText(
      el.querySelector('[data-testid="credential-risk"]')?.textContent ?? "",
    ),
    mismatch:
      el.querySelector('[data-testid="credential-mismatch"]') !== null,
    egress: el.querySelector('[data-testid="credential-egress"]') !== null,
  }));
}

function readChatTitle(container: HTMLElement): string | null {
  const title = container.querySelector<HTMLElement>(".chat-toolbar-title");
  return title ? tidyText(title.textContent) : null;
}

function readWorkers(container: HTMLElement): {
  visible: boolean;
  count: number | null;
} {
  const panel = container.querySelector<HTMLElement>('[data-testid="workers-panel"]');
  if (!panel) return { visible: false, count: null };
  const rows = panel.querySelectorAll<HTMLElement>(".worker-row");
  return { visible: true, count: rows.length };
}

function readInput(container: HTMLElement): AppView["input"] {
  const ta = container.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]');
  const sendBtn = container.querySelector<HTMLButtonElement>('[data-testid="send-btn"]');
  if (!ta) {
    return { present: false, text: "", disabled: true, sendDisabled: true };
  }
  return {
    present: true,
    text: ta.value,
    disabled: ta.disabled,
    sendDisabled: sendBtn?.disabled ?? true,
  };
}

export function extractView(container: HTMLElement): AppView {
  const messages = readMessages(container);
  const runs = readRuns(container);
  const workers = readWorkers(container);
  const chatVisible =
    container.querySelector('[data-testid="chat-container"]') !== null;
  const chatEl = container.querySelector<HTMLElement>(
    '[data-testid="chat-container"]',
  );
  const stopButton = container.querySelector<HTMLButtonElement>(".stop-btn");
  return {
    sidebar: {
      sessions: readSessions(container),
      workersPanelVisible: workers.visible,
      workerCount: workers.count,
    },
    chat: {
      visible: chatVisible,
      title: readChatTitle(container),
      messages,
      runs,
      running: chatEl?.dataset.sessionRunning === "true",
      stopButtonVisible: stopButton !== null,
      stopButtonDisabled: stopButton?.disabled ?? false,
      stopButtonStopping: stopButton?.classList.contains("stopping") ?? false,
      approvals: readApprovals(container),
      credentials: readCredentials(container),
    },
    input: readInput(container),
  };
}
