/** Contract doubles for the private extensions' frontend modules.
 *
 * The real modules are backend-served assets from the private repo
 * (`extensions/credential-broker/ui/credential-broker.entry.js`,
 * `extensions/team-orchestration/ui/team-sidebar.entry.js`), so the public
 * suite cannot import them. These doubles mirror the modules' DOM contract
 * (testids, classes, copy the suite asserts) and drive the REAL context
 * callbacks the app provides, so App/Chat wiring and the extension-backend
 * REST flows are exercised end to end. Contract tests for the real modules
 * live next to them in the private repo.
 *
 * `loadStubExtensionModule` replaces `loadExtensionModule` via the vi.mock
 * in tests/setup.ts (a test file's own vi.mock of the loader still wins).
 */
import type * as ReactRuntime from "react";

type ReactModule = typeof ReactRuntime;

interface ExtensionComponentProps {
  context: Record<string, unknown>;
  React: ReactModule;
}

interface CredentialSink {
  computed_host?: string;
  computed_target?: string;
  egress?: boolean;
  risk?: string;
  risk_reasons?: string[];
  label_mismatch?: boolean;
}

interface CredentialConsentLike {
  consent_id: string;
  label?: string;
  sink?: CredentialSink;
  secret_names?: string[];
  secret_sources?: Record<string, { service: string; account: string }>;
}

interface WorkerApprovalLike {
  delegation_id: string;
  justification?: string;
  instructions_preview?: string;
  proposed_description?: string;
  proposed_orchestration_mode?: string;
}

const TEAM_API = "/api/extensions/ofek-dev.team-orchestration/backend";

function CredentialConsentCard({
  React,
  consent,
  onApprove,
  onDeny,
}: {
  React: ReactModule;
  consent: CredentialConsentLike;
  onApprove: (consentId: string, secrets: Record<string, string>) => Promise<void>;
  onDeny: (consentId: string) => Promise<void>;
}) {
  const h = React.createElement;
  const { useState } = React;
  const secretNames = consent.secret_names?.length ? consent.secret_names : ["secret"];
  const secretSources = consent.secret_sources ?? {};
  const manualSecretNames = secretNames.filter((name) => !secretSources[name]);
  const [secrets, setSecrets] = useState<Record<string, string>>(() =>
    Object.fromEntries(manualSecretNames.map((name) => [name, ""])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sink = consent.sink ?? {};
  const canApprove = manualSecretNames.every((name) => secrets[name]);

  async function run(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return h(
    "div",
    {
      className: "worker-approval-card",
      "data-testid": "credential-consent-card",
      "data-consent-id": consent.consent_id,
    },
    h("div", { className: "worker-approval-header" }, "Credential request"),
    h("div", { className: "worker-approval-justification" }, consent.label ?? ""),
    h(
      "div",
      { className: "credential-sink", "data-testid": "credential-sink" },
      "Secret will be sent to: ",
      h("strong", null, sink.computed_target || sink.computed_host || "(unknown)"),
    ),
    h(
      "div",
      { className: "credential-flags" },
      h(
        "span",
        {
          "data-testid": "credential-risk",
          title: sink.risk_reasons?.join("; ") ?? "",
        },
        `risk: ${sink.risk || "unknown"}`,
      ),
      sink.egress
        ? h(
            "span",
            { "data-testid": "credential-egress" },
            "secret leaves this machine",
          )
        : null,
    ),
    sink.label_mismatch
      ? h(
          "div",
          { "data-testid": "credential-mismatch" },
          "Warning: the label mentions a different host than the real destination above. Verify before approving.",
        )
      : null,
    h(
      "div",
      { className: "worker-approval-fields" },
      secretNames.map((name) => {
        const source = secretSources[name];
        if (source) {
          return h(
            "div",
            { key: name, "data-testid": `credential-stored-secret-${name}` },
            `${name}: `,
            h("span", null, `${source.service}/${source.account}`),
          );
        }
        return h("input", {
          key: name,
          type: "password",
          "data-testid": "credential-secret-input",
          "data-secret-name": name,
          value: secrets[name] ?? "",
          onChange: (event: ReactRuntime.ChangeEvent<HTMLInputElement>) =>
            setSecrets((prev) => ({ ...prev, [name]: event.target.value })),
          placeholder:
            name === "secret" ? "Paste the secret value" : `Paste secret: ${name}`,
          autoComplete: "off",
          disabled: busy,
        });
      }),
    ),
    error ? h("div", { className: "worker-approval-resolved" }, error) : null,
    h(
      "div",
      { className: "worker-approval-buttons" },
      h(
        "button",
        {
          className: "deny",
          disabled: busy,
          onClick: () => run(() => onDeny(consent.consent_id)),
        },
        "Deny",
      ),
      h(
        "button",
        {
          className: "approve",
          disabled: busy || !canApprove,
          onClick: () => run(() => onApprove(consent.consent_id, secrets)),
        },
        manualSecretNames.length ? "Approve & store" : "Approve",
      ),
    ),
  );
}

const credentialBrokerModule = {
  Component({ context, React }: ExtensionComponentProps) {
    const h = React.createElement;
    const consents = Array.isArray(context.credentialConsents)
      ? (context.credentialConsents as CredentialConsentLike[])
      : [];
    const approve = context.approveCredential as (
      consentId: string,
      secrets: Record<string, string>,
    ) => Promise<void>;
    const deny = context.denyCredential as (consentId: string) => Promise<void>;
    return h(
      React.Fragment,
      null,
      consents.map((consent) =>
        h(CredentialConsentCard, {
          key: consent.consent_id,
          React,
          consent,
          onApprove: approve,
          onDeny: deny,
        }),
      ),
    );
  },
};

function WorkerApprovalCard({
  React,
  approval,
  approve,
  deny,
}: {
  React: ReactModule;
  approval: WorkerApprovalLike;
  approve: (delegationId: string, description: string, mode: string) => Promise<void>;
  deny: (delegationId: string) => Promise<void>;
}) {
  const h = React.createElement;
  const { useState } = React;
  const [description, setDescription] = useState(approval.proposed_description ?? "");
  const [mode, setMode] = useState(approval.proposed_orchestration_mode || "native");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return h(
    "div",
    {
      className: "worker-approval-card",
      "data-testid": "worker-approval-card",
      "data-delegation-id": approval.delegation_id,
    },
    h(
      "div",
      { className: "worker-approval-header" },
      "Team session wants to spawn a fresh worker",
    ),
    h("div", { className: "worker-approval-justification" }, approval.justification ?? ""),
    approval.instructions_preview
      ? h(
          "div",
          { className: "worker-approval-instructions" },
          approval.instructions_preview,
        )
      : null,
    h(
      "div",
      { className: "worker-approval-fields" },
      h("input", {
        type: "text",
        value: description,
        onChange: (event: ReactRuntime.ChangeEvent<HTMLInputElement>) =>
          setDescription(event.target.value),
        placeholder: "Worker description",
        disabled: busy,
      }),
      h(
        "select",
        {
          value: mode,
          onChange: (event: ReactRuntime.ChangeEvent<HTMLSelectElement>) =>
            setMode(event.target.value),
          disabled: busy,
        },
        h("option", { value: "native" }, "native"),
        h("option", { value: "team" }, "team"),
      ),
    ),
    error ? h("div", { className: "worker-approval-resolved" }, error) : null,
    h(
      "div",
      { className: "worker-approval-buttons" },
      h(
        "button",
        {
          className: "deny",
          disabled: busy,
          onClick: () => run(() => deny(approval.delegation_id)),
        },
        "Deny",
      ),
      h(
        "button",
        {
          className: "approve",
          disabled: busy || !description.trim(),
          onClick: () => run(() => approve(approval.delegation_id, description.trim(), mode)),
        },
        "Approve",
      ),
    ),
  );
}

function WorkerApprovals({ context, React }: ExtensionComponentProps) {
  const h = React.createElement;
  const approvals = Array.isArray(context.workerApprovals)
    ? (context.workerApprovals as WorkerApprovalLike[])
    : [];
  if (!approvals.length) return null;
  const approve = context.approveWorker as (
    delegationId: string,
    description: string,
    mode: string,
  ) => Promise<void>;
  const deny = context.denyWorker as (delegationId: string) => Promise<void>;
  return h(
    "div",
    { className: "worker-approval-stack" },
    approvals.map((approval) =>
      h(WorkerApprovalCard, {
        key: approval.delegation_id,
        React,
        approval,
        approve,
        deny,
      }),
    ),
  );
}

function WorkersPanel({ context, React }: ExtensionComponentProps) {
  const h = React.createElement;
  const { useCallback, useEffect, useState } = React;
  const apiBaseUrl = typeof context.apiBaseUrl === "string" ? context.apiBaseUrl : "";
  const cwd = typeof context.cwd === "string" ? context.cwd : "";
  const contextEvents = context.events;
  const [workers, setWorkers] = useState<{ worker_id?: string; description?: string }[]>([]);

  const refresh = useCallback(async () => {
    // apiBaseUrl === "" is the same-origin default (src/api.ts), not
    // "missing" — only cwd gates the fetch.
    if (!cwd) return;
    try {
      const res = await fetch(`${apiBaseUrl}${TEAM_API}/workers?cwd=${encodeURIComponent(cwd)}`);
      if (!res.ok) return;
      const data = await res.json();
      setWorkers(Array.isArray(data.workers) ? data.workers : []);
    } catch {
      // Mirror the real panel: a failed snapshot keeps the last list.
    }
  }, [apiBaseUrl, cwd]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const events = Array.isArray(contextEvents)
      ? (contextEvents as { type?: string }[])
      : [];
    if (!events.length) return;
    const last = events[events.length - 1];
    if (last?.type === "workers_changed") void refresh();
  }, [contextEvents, refresh]);

  return h(
    "div",
    { className: "workers-panel", "data-testid": "workers-panel" },
    workers.map((worker, index) =>
      h(
        "div",
        { key: worker.worker_id ?? index, className: "worker-row" },
        worker.description ?? worker.worker_id ?? "",
      ),
    ),
  );
}

const teamOrchestrationModule = {
  Component({ context, React }: ExtensionComponentProps) {
    if (context.slot === "chat-inline-actions") {
      return React.createElement(WorkerApprovals, { context, React });
    }
    return React.createElement(WorkersPanel, { context, React });
  },
};

function PromptEngineerStartModal({ context, React }: ExtensionComponentProps) {
  const h = React.createElement;
  const { useState } = React;
  const parentName = typeof context.parentName === "string" ? context.parentName : "";
  const parentHasClaudeSid = Boolean(context.parentHasClaudeSid);
  const onCancel = context.onCancel as () => void;
  const onPick = context.onPick as (mode: "fork" | "new") => Promise<void>;
  const [busy, setBusy] = useState(false);

  async function pick(mode: "fork" | "new") {
    if (busy) return;
    setBusy(true);
    try {
      await onPick(mode);
    } finally {
      setBusy(false);
    }
  }

  return h(
    "div",
    { className: "modal-overlay", onClick: onCancel },
    h(
      "div",
      { className: "modal-content", onClick: (event: Event) => event.stopPropagation() },
      h(
        "button",
        {
          type: "button",
          "data-testid": "prompt-eng-mode-fork",
          onClick: () => void pick("fork"),
          disabled: busy || !parentHasClaudeSid,
        },
        `Fork "${parentName}"`,
      ),
      h(
        "button",
        {
          type: "button",
          "data-testid": "prompt-eng-mode-new",
          onClick: () => void pick("new"),
          disabled: busy,
        },
        "Fresh session",
      ),
      h("button", { className: "setup-cancel-btn", onClick: onCancel, disabled: busy }, "Cancel"),
    ),
  );
}

function PromptEngineerOverlay({ context, React }: ExtensionComponentProps) {
  const h = React.createElement;
  const { useState } = React;
  const onSend = context.onSend as () => Promise<void>;
  const onCancel = context.onCancel as () => Promise<void>;
  const chatSlot = (context.chatSlot ?? null) as ReactRuntime.ReactNode;
  const fileViewerSlot = (context.fileViewerSlot ?? null) as ReactRuntime.ReactNode;
  const [busy, setBusy] = useState("");

  async function run(kind: string, fn: () => Promise<void>) {
    if (busy) return;
    setBusy(kind);
    try {
      await fn();
    } finally {
      setBusy("");
    }
  }

  return h(
    "div",
    { className: "working-mode-overlay", "data-testid": "prompt-eng-overlay" },
    h(
      "button",
      {
        type: "button",
        "data-testid": "prompt-eng-cancel-btn",
        onClick: () => void run("cancel", onCancel),
        disabled: Boolean(busy),
      },
      "Cancel",
    ),
    h(
      "div",
      { className: "prompt-eng-body" },
      h("div", { className: "prompt-eng-chat" }, chatSlot),
      h("div", { className: "prompt-eng-fileviewer" }, fileViewerSlot),
    ),
    h(
      "button",
      {
        type: "button",
        "data-testid": "prompt-eng-send-btn",
        onClick: () => void run("send", onSend),
        disabled: Boolean(busy),
      },
      "Send to parent",
    ),
  );
}

const promptEngineerModule = {
  Component({ context, React }: ExtensionComponentProps) {
    if (
      context.slot === "session-action-modal" &&
      context.activeModal === "prompt-engineer-start"
    ) {
      return React.createElement(PromptEngineerStartModal, { context, React });
    }
    if (
      context.slot === "session-workspace-overlay" &&
      context.activeOverlay === "prompt-engineer"
    ) {
      return React.createElement(PromptEngineerOverlay, { context, React });
    }
    return null;
  },
};

export async function loadStubExtensionModule(url: string): Promise<unknown> {
  if (url.endsWith("/credential-broker.entry.js")) return credentialBrokerModule;
  if (url.endsWith("/team-sidebar.entry.js")) return teamOrchestrationModule;
  if (url.endsWith("/prompt-engineer.entry.js")) return promptEngineerModule;
  throw new Error(
    `harness: no extension module stub for ${url} — add one to tests/harness/extensionModuleStubs.ts or vi.mock the loader in the test`,
  );
}
