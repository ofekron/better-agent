import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Capacitor } from "@capacitor/core";
import { readNativeServerUrl } from "../nativeServerConfig";
import { ChangeServerButton } from "./ChangeServer";

/** Settings section: shows the backend server this native client is
 * connected to and offers to change it. Renders nothing on web/desktop,
 * where the server is fixed to the origin serving the frontend. */
export function ServerSetting() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(() => readNativeServerUrl());
  useEffect(() => {
    const onFocus = () => setCurrent(readNativeServerUrl());
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);
  if (!Capacitor.isNativePlatform()) return null;

  return (
    <section className="settings-section server-setting">
      <div className="settings-section-header">
        <h3>{t("settings.serverTitle")}</h3>
        <p>{t("settings.serverSectionSubtitle")}</p>
      </div>
      <div className="server-setting-row">
        <div className="server-setting-current">
          <span className="server-setting-current-label">{t("settings.serverCurrent")}</span>
          <code className="server-setting-current-value">{current || "—"}</code>
        </div>
        <ChangeServerButton className="btn-secondary change-server-btn" />
      </div>
    </section>
  );
}
