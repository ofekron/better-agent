import { Capacitor } from "@capacitor/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchLineSwitchApps,
  lineSwitchAppUrl,
  lineSwitchLaunchUrl,
  readLineSwitchConnection,
  type LineSwitchAppPlatform,
} from "src/lineSwitchClient";
import { openExternalLink } from "src/utils/externalLink";
import Icon from "./Icon";

function platform(): LineSwitchAppPlatform {
  const native = Capacitor.getPlatform();
  if (native === "android" || native === "ios") return native;
  const client = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  if (/Mac/i.test(client)) return "macos";
  if (/Win/i.test(client)) return "windows";
  return "web";
}

function localRecoveryUrl(): string {
  const protocol = location.protocol === "https:" ? "https:" : "http:";
  const hostname = location.hostname && location.hostname !== "tauri.localhost" ? location.hostname : "127.0.0.1";
  return `${protocol}//${hostname}:18768/`;
}

export function OpenRecoveryAppButton({ className = "login-submit" }: { className?: string }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const open = async () => {
    setBusy(true);
    setError("");
    const connection = readLineSwitchConnection();
    let target = connection ? `${connection.baseUrl}/#${connection.token}` : localRecoveryUrl();
    try {
      if (connection) {
        const catalog = await fetchLineSwitchApps(connection);
        const candidates = catalog.apps.filter((app) => app.platforms.includes(platform()));
        const selected = candidates.find((app) => app.kind === "native") ?? candidates.find((app) => app.kind === "pwa");
        if (selected) target = selected.kind === "native"
          ? lineSwitchLaunchUrl(connection, selected)
          : lineSwitchAppUrl(connection, selected);
      }
      await openExternalLink(target);
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
