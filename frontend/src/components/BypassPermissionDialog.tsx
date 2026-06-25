import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

interface Props {
  open: boolean;
  /** "Send anyway" — acknowledge and proceed with the pending prompt. */
  onSendAnyway: () => void;
  /** "Change in Settings" — don't send, jump to provider settings. */
  onChangeInSettings: () => void;
  /** Plain dismiss (overlay / X) — don't send, don't acknowledge. The dialog
   * reappears on the next send until the user explicitly sends anyway. */
  onDismiss: () => void;
}

/** One-time warning that the session runs with permissions fully bypassed.
 * Shown on the first prompt send until the user acknowledges by sending. */
export function BypassPermissionDialog({ open, onSendAnyway, onChangeInSettings, onDismiss }: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onDismiss);
  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onDismiss}>
      <div className="modal-content" style={{ maxWidth: "440px" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t("bypassPermission.title")}</h2>
          <button className="modal-close" onClick={onDismiss}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          <p style={{ margin: "16px 0", lineHeight: "1.5", color: "var(--text-secondary)" }}>
            {t("bypassPermission.message")}
          </p>
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onChangeInSettings}>
            {t("bypassPermission.changeInSettings")}
          </button>
          <button type="button" className="btn-danger" onClick={onSendAnyway} autoFocus>
            {t("bypassPermission.sendAnyway")}
          </button>
        </div>
      </div>
    </div>
  );
}
