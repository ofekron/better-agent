import { useCallback, useEffect, useMemo, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { useTranslation } from "react-i18next";
import {
  clearLineSwitchConnection,
  fetchLineSwitchApps,
  lineSwitchAppUrl,
  parseLineSwitchAccessUrl,
  readLineSwitchConnection,
  writeLineSwitchConnection,
  type LineSwitchApp,
  type LineSwitchAppCatalog,
  type LineSwitchAppPlatform,
  type LineSwitchConnection,
} from "src/lineSwitchClient";
import { openExternalLink } from "src/utils/externalLink";
import Icon from "./Icon";

function basCompanionPlatform(): LineSwitchAppPlatform {
  const native = Capacitor.getPlatform();
  if (native === "android" || native === "ios") return native;
  const client = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  if (/Mac/i.test(client)) return "macos";
  if (/Win/i.test(client)) return "windows";
  return "web";
}

function appsForPlatform(catalog: LineSwitchAppCatalog | null, platform: LineSwitchAppPlatform): LineSwitchApp[] {
  return catalog?.apps.filter((app) => app.platforms.includes(platform)) ?? [];
}

export function BasCompanionAppsSetting() {
  const { t } = useTranslation();
  const [connection, setConnection] = useState<LineSwitchConnection | null>(readLineSwitchConnection);
  const [catalog, setCatalog] = useState<LineSwitchAppCatalog | null>(null);
  const [accessUrl, setAccessUrl] = useState("");
  const [busy, setBusy] = useState(() => connection !== null);
  const [error, setError] = useState("");
  const platform = useMemo(() => basCompanionPlatform(), []);
  const apps = useMemo(() => appsForPlatform(catalog, platform), [catalog, platform]);

  const load = useCallback(async (next: LineSwitchConnection) => {
    setBusy(true);
    setError("");
    try {
      setCatalog(await fetchLineSwitchApps(next));
      return true;
    } catch (cause) {
      setCatalog(null);
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!connection || catalog) return;
    let cancelled = false;
    void fetchLineSwitchApps(connection).then((value) => {
      if (cancelled) return;
      setCatalog(value);
      setError("");
    }).catch((cause) => {
      if (cancelled) return;
      setError(cause instanceof Error ? cause.message : String(cause));
    }).finally(() => {
      if (!cancelled) setBusy(false);
    });
    return () => {
      cancelled = true;
    };
  }, [catalog, connection]);

  const pair = async () => {
    let next: LineSwitchConnection;
    try {
      next = parseLineSwitchAccessUrl(accessUrl);
    } catch {
      setError(t("serverSetup.urlInvalid"));
      return;
    }
    if (!await load(next)) return;
    writeLineSwitchConnection(next);
    setConnection(next);
    setAccessUrl("");
  };

  const forget = () => {
    clearLineSwitchConnection();
    setConnection(null);
    setCatalog(null);
    setError("");
  };

  const openApp = async (app: LineSwitchApp) => {
    setError("");
    try {
      await openExternalLink(lineSwitchAppUrl(connection!, app));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <section className={`bas-companion-setting${busy ? " is-busy" : ""}`} aria-busy={busy}>
      <div className="bas-companion-header">
        <div>
          <h3>{t("settings.recoveryTitle")}</h3>
          <p>{t("settings.recoverySubtitle")}</p>
        </div>
        {connection && (
          <button type="button" className="btn-secondary" onClick={forget}>
            {t("backendUnavailable.changeServer")}
          </button>
        )}
      </div>

      {!connection ? (
        <div className="bas-companion-pair">
          <p>{t("settings.recoveryPairHint")}</p>
          <label htmlFor="bas-companion-access">{t("serverSetup.urlLabel")}</label>
          <div className="bas-companion-pair-row">
            <input
              id="bas-companion-access"
              value={accessUrl}
              onChange={(event) => setAccessUrl(event.target.value)}
              placeholder="https://host:18768/#access-key"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
            <button type="button" className="setup-save-btn" disabled={busy || !accessUrl.trim()} onClick={() => void pair()}>
              {busy ? t("serverSetup.testing") : t("serverSetup.connect")}
            </button>
          </div>
        </div>
      ) : (
        <div className="bas-companion-apps">
          {busy && !catalog && <div className="bas-companion-loading" role="status">{t("app.loading")}</div>}
          {!busy && catalog && apps.length === 0 && <p>{t("settings.recoveryNoApps")}</p>}
          {apps.map((app) => (
            <article className="bas-companion-app" key={app.id}>
              <span className="bas-companion-app-icon"><Icon name="target" size={20} /></span>
              <div>
                <strong>{app.label}</strong>
                <small>{app.kind === "pwa" ? t("settings.recoveryWebApp") : platform}</small>
              </div>
              <button type="button" className="setup-save-btn" onClick={() => void openApp(app)}>
                {t("settings.recoveryOpenInstall")}
              </button>
            </article>
          ))}
          {error && (
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => void load(connection)}>
              {t("backendUnavailable.retry")}
            </button>
          )}
        </div>
      )}
      {error && <div className="settings-error" role="alert">{error}</div>}
    </section>
  );
}
