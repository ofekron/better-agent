import { useState } from "react";
import { useTranslation } from "react-i18next";
import { dismissSyncFailure, useSyncStatus } from "../progress/store";

export function SyncFailureToast() {
  const { t } = useTranslation();
  const { failures } = useSyncStatus();
  const [expanded, setExpanded] = useState<string | null>(null);
  if (failures.length === 0) return null;

  return (
    <aside className="sync-failure-stack" aria-live="assertive">
      {failures.map((failure) => {
        const isExpanded = expanded === failure.correlationId;
        return (
          <article className="sync-failure-toast" role="alert" key={failure.correlationId}>
            <div className="sync-failure-toast__mark" aria-hidden="true">!</div>
            <div className="sync-failure-toast__body">
              <strong>{t("sync.failureTitle", { action: failure.action })}</strong>
              <span>{failure.info || t("sync.failureReconciled")}</span>
              {isExpanded && <pre>{failure.details}</pre>}
              <button type="button" onClick={() => setExpanded(isExpanded ? null : failure.correlationId)}>
                {t(isExpanded ? "sync.hideDetails" : "sync.showDetails")}
              </button>
            </div>
            <button
              type="button"
              className="sync-failure-toast__dismiss"
              aria-label={t("sync.dismiss")}
              onClick={() => dismissSyncFailure(failure.correlationId)}
            >×</button>
          </article>
        );
      })}
    </aside>
  );
}
