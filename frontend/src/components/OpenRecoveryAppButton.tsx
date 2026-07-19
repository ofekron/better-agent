import { Capacitor } from "@capacitor/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { openExternalLink } from "src/utils/externalLink";
import Icon from "./Icon";

const LOCAL_RECOVERY_URL = "http://127.0.0.1:18768/";
const COMPANION_DEEP_LINK = "betteragentswitch://open";

export function OpenRecoveryAppButton({ className = "login-submit" }: { className?: string }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const open = async () => {
    setBusy(true);
    setError("");
    try {
      await openExternalLink(Capacitor.isNativePlatform() ? COMPANION_DEEP_LINK : LOCAL_RECOVERY_URL);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="open-recovery-app">
      <button className={className} type="button" disabled={busy} onClick={() => void open()}>
        <Icon name="target" size={16} />
        {busy ? t("app.loading") : t("settings.openRecoveryApp")}
      </button>
      {error && <div className="settings-error" role="alert">{error}</div>}
    </div>
  );
}
