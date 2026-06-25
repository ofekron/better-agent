import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { Project } from "../types";

interface Props {
  sessionName: string;
  currentCwd: string;
  projects: Project[];
  busy: boolean;
  error?: string | null;
  onConfirm: (cwd: string) => void;
  onCancel: () => void;
}

/** Picker for "Move to project": creates a continuation session in the
 * chosen project and archives the source session (backend-owned flow). */
export function MoveSessionModal({
  sessionName,
  currentCwd,
  projects,
  busy,
  error,
  onConfirm,
  onCancel,
}: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(true, onCancel);
  const targets = projects.filter((p) => p.path !== currentCwd);

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onCancel}>
      <div
        className="modal-content move-session-modal"
        style={{ maxWidth: "440px" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>{t("moveSession.title")}</h2>
          <button className="modal-close" onClick={onCancel} disabled={busy}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          <p
            style={{
              lineHeight: "1.5",
              color: "var(--text-secondary)",
              overflowWrap: "anywhere",
              margin: "12px 0",
            }}
          >
            {t("moveSession.description", { session: sessionName })}
          </p>
          {error && (
            <p style={{ color: "var(--error, #d9534f)", overflowWrap: "anywhere" }}>
              {error}
            </p>
          )}
          {busy ? (
            <p style={{ color: "var(--text-secondary)" }}>
              {t("moveSession.moving")}
            </p>
          ) : targets.length === 0 ? (
            <p style={{ color: "var(--text-secondary)" }}>
              {t("moveSession.noProjects")}
            </p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {targets.map((p) => (
                <li key={p.path}>
                  <button
                    type="button"
                    className="modal-list-item"
                    style={{
                      display: "block",
                      width: "100%",
                      textAlign: "start",
                      padding: "8px 10px",
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      color: "var(--text-primary)",
                      borderRadius: "6px",
                    }}
                    onClick={() => onConfirm(p.path)}
                  >
                    <div>{p.name}</div>
                    <div
                      style={{
                        fontSize: "12px",
                        color: "var(--text-secondary)",
                        overflowWrap: "anywhere",
                      }}
                    >
                      {p.path}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
