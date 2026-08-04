import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { NodeProviderCredentialStatus as CredentialStatus, NodeSnapshot } from "../types";


interface Props {
  machines: NodeSnapshot[];
}


type VisibleStatus = CredentialStatus & {
  dismissalKey: string;
  signature: string;
};


function actionableStatuses(machines: NodeSnapshot[]): VisibleStatus[] {
  return machines.flatMap((machine) =>
    (machine.provider_credentials ?? [])
      .filter((status) => status.status !== "synced")
      .map((status) => ({
        ...status,
        dismissalKey: [machine.id, status.provider_id].join(":"),
        signature: [
          machine.id,
          status.provider_id,
          status.status,
          status.updated_at,
        ].join(":"),
      })),
  );
}


export function NodeProviderCredentialStatus({ machines }: Props) {
  const { t } = useTranslation();
  const [dismissedAt, setDismissedAt] = useState<Record<string, string>>({});
  const statuses = useMemo(() => actionableStatuses(machines), [machines]);
  const visible = statuses.filter(
    (status) =>
      status.status === "pending"
      || dismissedAt[status.dismissalKey] !== status.updated_at,
  );

  if (!visible.length) return null;

  return (
    <aside
      className="node-provider-credential-status"
      role="status"
      aria-live="polite"
    >
      <ul>
        {visible.map((status) => (
          <li
            key={status.signature}
            className={`node-provider-credential-status-${status.status}`}
          >
            <span className="node-provider-credential-status-indicator" aria-hidden="true" />
            <span>
              {t(
                status.status === "pending"
                  ? "nodeCredentials.syncing"
                  : "nodeCredentials.failed",
                {
                  provider: status.provider_name,
                  node: status.node_id,
                },
              )}
            </span>
            {status.status === "failed" ? (
              <button
                type="button"
                onClick={() => {
                  setDismissedAt((current) => ({
                    ...current,
                    [status.dismissalKey]: status.updated_at,
                  }));
                }}
                aria-label={t("nodeCredentials.dismiss", {
                  provider: status.provider_name,
                  node: status.node_id,
                })}
              >
                ×
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </aside>
  );
}
