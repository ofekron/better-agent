import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import {
  REFRESH_CONTEXT_STORAGE_KEY,
  type RefreshContext,
} from "../lib/refreshContext";

declare const __BUILD_HASH__: string;
declare const __BUILD_TIME__: string;

interface BuildInfo {
  git_hash: string | null;
  refresh_result?: {
    request_id: string;
    status: "succeeded" | "failed";
    completed_at: string;
    error: string | null;
  } | null;
}

const TOAST_MS = 8000;

export function RefreshResult() {
  const { t } = useTranslation();
  const [result, setResult] = useState<{
    previousHash: string;
    currentHash: string;
    serverHash: string | null;
    buildTime: string;
    refreshTime: number;
    buildStatus: "succeeded" | "failed" | "unknown";
    buildError: string | null;
  } | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [visible, setVisible] = useState(false);
  useBackButtonDismiss(modalOpen, () => setModalOpen(false));

  useEffect(() => {
    const raw = localStorage.getItem(REFRESH_CONTEXT_STORAGE_KEY);
    if (!raw) return;
    let ctx: RefreshContext;
    try { ctx = JSON.parse(raw) } catch { localStorage.removeItem(REFRESH_CONTEXT_STORAGE_KEY); return }
    localStorage.removeItem(REFRESH_CONTEXT_STORAGE_KEY);

    const currentHash = __BUILD_HASH__;
    const buildTime = __BUILD_TIME__;

    // Fetch server-side hash for completeness.
    fetch(`${API}/api/build-info`, { cache: "no-store" })
      .then((r) => r.ok ? r.json() as Promise<BuildInfo> : { git_hash: null })
      .then((info) => {
        const refreshResult = info.refresh_result?.request_id === ctx.requestId
          ? info.refresh_result
          : null;
        setResult({
          previousHash: ctx.previousHash,
          currentHash,
          serverHash: info.git_hash,
          buildTime,
          refreshTime: ctx.refreshTime,
          buildStatus: refreshResult?.status ?? "unknown",
          buildError: refreshResult?.error ?? null,
        });
        setVisible(true);
        setTimeout(() => setVisible(false), TOAST_MS);
      })
      .catch(() => {
        setResult({
          previousHash: ctx.previousHash,
          currentHash,
          serverHash: null,
          buildTime,
          refreshTime: ctx.refreshTime,
          buildStatus: "unknown",
          buildError: null,
        });
        setVisible(true);
        setTimeout(() => setVisible(false), TOAST_MS);
      });
  }, []);

  if (!result) return null;

  const updated = result.previousHash !== result.currentHash;
  const failed = result.buildStatus === "failed";
  const label = failed
    ? t("refreshResult.failed", { hash: result.currentHash })
    : updated
      ? t("refreshResult.updated", { hash: result.currentHash })
      : result.buildStatus === "succeeded"
        ? t("refreshResult.succeededSameVersion", { hash: result.currentHash })
        : t("refreshResult.sameVersion", { hash: result.currentHash });

  return (
    <>
      <div className={`refresh-toast ${visible ? "visible" : ""}`}>
        <span className={`refresh-toast-icon ${failed ? "failed" : updated ? "ok" : "same"}`}>
          {failed ? "!" : updated ? "✓" : "="}
        </span>
        <span className="refresh-toast-label">{label}</span>
        <button
          className="refresh-toast-details"
          onClick={() => { setVisible(false); setModalOpen(true) }}
        >
          {t("refreshResult.details")}
        </button>
        <button className="refresh-toast-close" onClick={() => setVisible(false)}>
          ×
        </button>
      </div>

      {modalOpen && (
        <div className="modal-overlay" onClick={() => setModalOpen(false)}>
          <div className="modal-content" style={{ maxWidth: "420px" }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{t("refreshResult.modalTitle")}</h2>
              <button className="modal-close" onClick={() => setModalOpen(false)}>
                ×
              </button>
            </div>
            <div className="modal-body">
              <table className="refresh-details-table">
                <tbody>
                  <tr>
                    <td className="refresh-details-key">{t("refreshResult.status")}</td>
                    <td className={failed ? "refresh-details-failed" : "refresh-details-updated"}>
                      {failed
                        ? t("refreshResult.statusFailed")
                        : result.buildStatus === "succeeded"
                          ? t("refreshResult.statusSucceeded")
                          : updated
                            ? t("refreshResult.statusUpdated")
                            : t("refreshResult.statusUnknown")}
                    </td>
                  </tr>
                  <tr>
                    <td className="refresh-details-key">{t("refreshResult.previousVersion")}</td>
                    <td><code>{result.previousHash}</code></td>
                  </tr>
                  <tr>
                    <td className="refresh-details-key">{t("refreshResult.currentVersion")}</td>
                    <td><code>{result.currentHash}</code></td>
                  </tr>
                  {result.serverHash && (
                    <tr>
                      <td className="refresh-details-key">{t("refreshResult.serverVersion")}</td>
                      <td><code>{result.serverHash}</code></td>
                    </tr>
                  )}
                  <tr>
                    <td className="refresh-details-key">{t("refreshResult.builtAt")}</td>
                    <td>{new Date(result.buildTime).toLocaleString()}</td>
                  </tr>
                  <tr>
                    <td className="refresh-details-key">{t("refreshResult.refreshedAt")}</td>
                    <td>{new Date(result.refreshTime).toLocaleString()}</td>
                  </tr>
                </tbody>
              </table>
              {failed && result.buildError && (
                <pre className="refresh-details-error">{result.buildError}</pre>
              )}
              {!updated && result.buildStatus === "succeeded" && (
                <p className="refresh-details-note">
                  {t("refreshResult.noChangeNote")}
                </p>
              )}
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setModalOpen(false)}>
                {t("app.cancel")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
