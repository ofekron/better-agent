import { useTranslation } from "react-i18next";
import { useSyncStatus } from "../progress/store";

export function SyncStatusIndicator() {
  const { t } = useTranslation();
  const { pendingCount } = useSyncStatus();
  if (pendingCount === 0) return null;

  return (
    <div className="sync-status-indicator" role="status" aria-live="polite">
      <span aria-hidden="true" />
      {t("sync.pending", { count: pendingCount })}
    </div>
  );
}
