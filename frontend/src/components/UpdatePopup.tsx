import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ApkUpdater } from "../apkUpdater";
import { getStoredToken } from "../bearerAuth";
import { API } from "../api";

interface Props {
  versionName: string | null;
  onDismiss: () => void;
}

/** In-app APK update prompt (native only). Triggers the native
 * ApkUpdater plugin, which downloads the APK and hands it to Android's
 * package installer. The OS still shows its own Install confirmation. */
export function UpdatePopup({ versionName, onDismiss }: Props) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const update = async () => {
    setBusy(true);
    setErr(null);
    try {
      await ApkUpdater.downloadAndInstall({
        url: `${API}/api/download/android`,
        token: getStoredToken() ?? "",
      });
      // Handed off to the installer — stop nagging for this build.
      onDismiss();
    } catch (e) {
      setErr(t("update.error"));
      console.error("apk update failed", e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="update-popup-overlay" role="dialog" aria-modal="true">
      <div className="update-popup-card">
        <h2 className="update-popup-title">{t("update.title")}</h2>
        <p className="update-popup-body">
          {t("update.body", { version: versionName ?? "" })}
        </p>
        {err && <div className="update-popup-error" role="alert">{err}</div>}
        <div className="update-popup-actions">
          <button
            type="button"
            className="update-popup-btn secondary"
            onClick={onDismiss}
            disabled={busy}
          >
            {t("update.later")}
          </button>
          <button
            type="button"
            className="update-popup-btn primary"
            onClick={update}
            disabled={busy}
          >
            {busy ? t("update.installing") : t("update.updateNow")}
          </button>
        </div>
      </div>
    </div>
  );
}
