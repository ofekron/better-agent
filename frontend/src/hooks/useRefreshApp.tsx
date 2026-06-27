import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { uuidv4 } from "../lib/uuid";
import { clearRefreshContext, saveRefreshContext } from "../lib/refreshContext";
import { hardRefreshCurrentPage } from "../lib/hardRefresh";
import { RefreshChoiceModal, type RefreshMode } from "../components/RefreshChoiceModal";

type RestartStatus = {
  accepted: boolean;
  refresh_result?: { request_id?: string } | null;
};

export interface UseRefreshApp {
  restarting: boolean;
  restartError: string | null;
  dismissRestartError: () => void;
  openRefreshModal: () => void;
  refreshModal: ReactNode;
}

/** Owns the prod-mode refresh flow: opens the choice modal, POSTs
 *  /api/admin/restart (which SIGTERMs the backend), polls
 *  /api/admin/restart-status until the matching supervised build result is
 *  available, then hard-reloads so the browser pulls the new bundle. Returned
 *  `refreshModal` is the element to render; shared by the main app and the
 *  standalone settings window so the flow has a single implementation. */
export function useRefreshApp(): UseRefreshApp {
  const { t } = useTranslation();
  const [restarting, setRestarting] = useState(false);
  const [refreshModalOpen, setRefreshModalOpen] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);

  const openRefreshModal = useCallback(() => {
    if (restarting) return;
    setRefreshModalOpen(true);
  }, [restarting]);

  const dismissRestartError = useCallback(() => setRestartError(null), []);

  const handleRefreshApp = useCallback(
    async (mode: RefreshMode) => {
      if (restarting) return;
      setRefreshModalOpen(false);
      setRestartError(null);
      setRestarting(true);
      const requestId = uuidv4();
      saveRefreshContext(requestId);
      let accepted = false;
      try {
        const res = await fetch(`${API}/api/admin/restart`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: requestId, mode }),
        });
        if (!res.ok) {
          clearRefreshContext();
          setRestarting(false);
          let detail = "";
          try {
            detail = ((await res.json()) as { detail?: string })?.detail ?? "";
          } catch {
            /* non-JSON body — fall back to the generic i18n message */
          }
          setRestartError(detail || t("app.refreshUnavailable"));
          return;
        }
        accepted = true;
      } catch {
        accepted = false;
      }
      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 500));
        try {
          const res = await fetch(
            `${API}/api/admin/restart-status/${encodeURIComponent(requestId)}`,
            { cache: "no-store" },
          );
          const info = res.ok ? ((await res.json()) as RestartStatus) : null;
          if (!accepted && info?.accepted === false) {
            clearRefreshContext();
            setRestarting(false);
            setRestartError(t("app.refreshNotAccepted"));
            return;
          }
          if (info?.accepted) {
            accepted = true;
          }
          if (info?.refresh_result?.request_id === requestId) {
            await hardRefreshCurrentPage(requestId);
            return;
          }
        } catch {
          // Backend still down — keep polling.
        }
      }
      // Gave up waiting; surface the failure and let the user retry.
      clearRefreshContext();
      setRestarting(false);
      setRestartError(t("app.refreshTimeout"));
    },
    [restarting, t],
  );

  const refreshModal = refreshModalOpen ? (
    <RefreshChoiceModal
      onRefresh={(mode) => void handleRefreshApp(mode)}
      onClose={() => setRefreshModalOpen(false)}
    />
  ) : null;

  return {
    restarting,
    restartError,
    dismissRestartError,
    openRefreshModal,
    refreshModal,
  };
}
