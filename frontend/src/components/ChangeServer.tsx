import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Capacitor } from "@capacitor/core";
import { ServerSetup } from "./ServerSetup";
import { readNativeServerUrl } from "../nativeServerConfig";

interface Props {
  /** Extra class on the trigger button. */
  className?: string;
  /** Label for the trigger button. Defaults to the shared
   * "Change server" string used across the app. */
  label?: string;
}

/** Native-only trigger that opens the server picker in a modal.
 *
 * Web/desktop builds are same-origin (server is fixed to the origin
 * serving the frontend), so this renders nothing there. On Capacitor
 * native the backend URL is a runtime choice, so the user can re-point
 * the app at a different backend from anywhere this control is mounted
 * (login screen, settings). On a successful test+save the page reloads
 * so `API`/`WS_URL` re-resolve against the new origin. */
export function ChangeServerButton({ className, label }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (!Capacitor.isNativePlatform()) return null;

  return (
    <>
      <button
        type="button"
        className={className ?? "login-submit change-server-btn"}
        style={{ marginTop: 10, background: "var(--bg-input)", color: "var(--text-primary)" }}
        onClick={() => setOpen(true)}
      >
        {label ?? t("backendUnavailable.changeServer")}
      </button>
      {open && (
        <div className="modal-overlay" onClick={() => setOpen(false)}>
          <div
            className="modal-content change-server-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <ServerSetup
              initialUrl={readNativeServerUrl()}
              onConfigured={() => window.location.reload()}
            />
          </div>
        </div>
      )}
    </>
  );
}
