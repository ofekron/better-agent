/** Harness stand-in for the chat-inline-actions extension module.
 *
 * The real worker-approval and credential-consent cards live in installed
 * extensions (ofek-dev.team-orchestration / ofek-dev.credential-broker)
 * whose bundles are not part of this repo. Core tests lock the CORE
 * contract — Chat's REST rehydrate + WS invalidation + the context it
 * hands to the slot — so the harness mounts this stub through the real
 * ExtensionModuleSlot machinery and drives the same context callbacks the
 * production modules use. */

import { useState } from "react";
import type { CredentialConsent, PendingApproval } from "../../src/types";

export const HARNESS_INLINE_ACTIONS_URL =
  "/api/extensions/test-harness.inline-actions/frontend/ui/chat-inline-actions.entry.js";

interface InlineActionsContext {
  workerApprovals?: PendingApproval[];
  approveWorker?: (
    delegationId: string,
    description: string,
    orchestrationMode: string,
  ) => Promise<void>;
  denyWorker?: (delegationId: string) => Promise<void>;
  credentialConsents?: CredentialConsent[];
  approveCredential?: (
    consentId: string,
    secrets: Record<string, string>,
  ) => Promise<void>;
  denyCredential?: (consentId: string) => Promise<void>;
}

function WorkerApprovalStubCard({
  approval,
  context,
}: {
  approval: PendingApproval;
  context: InlineActionsContext;
}) {
  const [description, setDescription] = useState(
    approval.proposed_description ?? "",
  );
  return (
    <div
      data-testid="worker-approval-card"
      data-delegation-id={approval.delegation_id}
    >
      <div>{approval.justification}</div>
      <div>{approval.instructions_preview}</div>
      <div>{approval.model}</div>
      <input
        type="text"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <button
        type="button"
        className="approve"
        onClick={() =>
          void context.approveWorker?.(
            approval.delegation_id,
            description,
            approval.proposed_orchestration_mode,
          )
        }
      >
        Approve
      </button>
      <button
        type="button"
        className="deny"
        onClick={() => void context.denyWorker?.(approval.delegation_id)}
      >
        Deny
      </button>
    </div>
  );
}

function CredentialConsentStubCard({
  consent,
  context,
}: {
  consent: CredentialConsent;
  context: InlineActionsContext;
}) {
  const [secret, setSecret] = useState("");
  const storedNames = new Set(
    Object.entries(consent.secret_sources ?? {})
      .filter(([, source]) => source?.kind === "password_manager")
      .map(([name]) => name),
  );
  const unstoredNames = (consent.secret_names ?? []).filter(
    (name) => !storedNames.has(name),
  );
  return (
    <div
      data-testid="credential-consent-card"
      data-consent-id={consent.consent_id}
    >
      <div>{consent.label}</div>
      <div data-testid="credential-sink">{consent.sink.computed_target}</div>
      <div data-testid="credential-risk">{consent.sink.risk}</div>
      {consent.sink.label_mismatch && (
        <div data-testid="credential-mismatch">label mismatch</div>
      )}
      {consent.sink.egress && <div data-testid="credential-egress">egress</div>}
      {[...storedNames].map((name) => {
        const source = consent.secret_sources?.[name];
        return (
          <div key={name}>
            {source?.service}/{source?.account}
          </div>
        );
      })}
      {unstoredNames.length > 0 && (
        <input
          data-testid="credential-secret-input"
          type="password"
          placeholder="Paste secret"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
      )}
      <button
        type="button"
        className="approve"
        onClick={() =>
          void context.approveCredential?.(
            consent.consent_id,
            secret && unstoredNames.length > 0
              ? { [unstoredNames[0]]: secret }
              : {},
          )
        }
      >
        Approve
      </button>
      <button
        type="button"
        className="deny"
        onClick={() => void context.denyCredential?.(consent.consent_id)}
      >
        Deny
      </button>
    </div>
  );
}

function InlineActionsStub({ context }: { context: InlineActionsContext }) {
  return (
    <div className="worker-approval-stack">
      {(context.workerApprovals ?? []).map((approval) => (
        <WorkerApprovalStubCard
          key={approval.delegation_id}
          approval={approval}
          context={context}
        />
      ))}
      {(context.credentialConsents ?? []).map((consent) => (
        <CredentialConsentStubCard
          key={consent.consent_id}
          consent={consent}
          context={context}
        />
      ))}
    </div>
  );
}

/** Modules the harness serves through the (test-mocked) extension module
 * loader, keyed by the exact module_url advertised in the mock backend's
 * /api/extensions/frontend-entrypoints payload. */
export const harnessExtensionModules: Record<
  string,
  { Component: (props: { context: Record<string, unknown> }) => unknown }
> = {
  [HARNESS_INLINE_ACTIONS_URL]: {
    Component: ({ context }) => (
      <InlineActionsStub context={context as InlineActionsContext} />
    ),
  },
};
