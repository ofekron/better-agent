import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { PreSendAdvisory } from "../utils/preSendAdvisory";

interface Props {
  open: boolean;
  advisories: PreSendAdvisory[];
  /** "Send anyway" — proceed with the pending prompt despite the advisories. */
  onSendAnyway: () => void;
  /** Cancel/dismiss — don't send; the draft stays in the composer so the
   * user can switch provider/model and retry. */
  onCancel: () => void;
  /** Snooze this dialog for the current (provider, model) for 5 hours and
   * proceed to send. */
  onSnoozeFiveHours: () => void;
}

function formatResetsAt(resetsAt: string | undefined): string | null {
  if (!resetsAt) return null;
  const date = new Date(resetsAt);
  if (isNaN(date.getTime())) return null;
  return date.toLocaleString();
}

/** Shown before a prompt is sent when an extension reports a pre-send
 * advisory (e.g. provider quota nearly exhausted). */
export function PreSendAdvisoryDialog({ open, advisories, onSendAnyway, onCancel, onSnoozeFiveHours }: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onCancel);
  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-content" style={{ maxWidth: "480px" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t("preSendAdvisory.title")}</h2>
          <button className="modal-close" onClick={onCancel}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          <p style={{ margin: "12px 0", lineHeight: "1.5", color: "var(--text-secondary)" }}>
            {t("preSendAdvisory.intro")}
          </p>
          <ul style={{ margin: "12px 0", paddingInlineStart: "20px", lineHeight: "1.6" }}>
            {advisories.map((advisory, index) => {
              const resets = formatResetsAt(advisory.resets_at);
              return (
                <li key={`${advisory.extension_id}-${index}`} style={{ marginBottom: "8px" }}>
                  <strong>{advisory.title}</strong>
                  {advisory.detail && (
                    <div style={{ color: "var(--text-secondary)" }}>{advisory.detail}</div>
                  )}
                  {resets && (
                    <div style={{ color: "var(--text-secondary)" }}>
                      {t("preSendAdvisory.resetsAt", { time: resets })}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          <p style={{ margin: "12px 0", fontSize: "0.85em", color: "var(--text-secondary)" }}>
            {t("preSendAdvisory.approximate")}
          </p>
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onCancel}>
            {t("preSendAdvisory.cancel")}
          </button>
          <button type="button" className="btn-secondary" onClick={onSnoozeFiveHours}>
            {t("preSendAdvisory.snoozeFiveHours")}
          </button>
          <button type="button" className="btn-danger" onClick={onSendAnyway} autoFocus>
            {t("preSendAdvisory.sendAnyway")}
          </button>
        </div>
      </div>
    </div>
  );
}
