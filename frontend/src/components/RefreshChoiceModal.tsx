import { useTranslation } from "react-i18next";
import Icon from "./Icon";

export type RefreshMode = "now" | "idle";

interface Props {
  onRefresh: (mode: RefreshMode) => void;
  onClose: () => void;
}

/** Modal that lets the user pick how to apply a backend restart — now or
 *  when idle. Shared by the main app and the standalone settings window so
 *  both drive the single refresh flow in `useRefreshApp`. */
export function RefreshChoiceModal({ onRefresh, onClose }: Props) {
  const { t } = useTranslation();
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content refresh-choice-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="refresh-choice-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id="refresh-choice-title">{t("app.refreshModalTitle")}</h2>
          <button
            className="modal-close"
            onClick={onClose}
            aria-label={t("startup_tasks.dismiss")}
          >
            &times;
          </button>
        </div>
        <div className="modal-body refresh-choice-body">
          <button
            type="button"
            className="refresh-choice-option"
            onClick={() => onRefresh("now")}
          >
            <span className="refresh-choice-icon">
              <Icon name="refresh" size={18} />
            </span>
            <span>
              <strong>{t("app.refreshNowTitle")}</strong>
              <small>{t("app.refreshNowDescription")}</small>
            </span>
          </button>
          <button
            type="button"
            className="refresh-choice-option"
            onClick={() => onRefresh("idle")}
          >
            <span className="refresh-choice-icon">
              <Icon name="clock" size={18} />
            </span>
            <span>
              <strong>{t("app.refreshIdleTitle")}</strong>
              <small>{t("app.refreshIdleDescription")}</small>
            </span>
          </button>
        </div>
        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>
            {t("app.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
