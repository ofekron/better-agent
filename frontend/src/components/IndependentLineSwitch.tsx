import { useCallback, useEffect, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { useTranslation } from "react-i18next";
import {
  clearLineSwitchConnection,
  fetchLineSwitchState,
  parseLineSwitchAccessUrl,
  readLineSwitchConnection,
  requestLineSwitch,
  targetServerUrl,
  writeLineSwitchConnection,
  type LineSwitchConnection,
  type LineSwitchState,
} from "src/lineSwitchClient";
import { clearStoredToken } from "src/bearerAuth";
import { writeNativeServerUrl } from "src/nativeServerConfig";

export function IndependentLineSwitch() {
  const { t } = useTranslation();
  const [connection, setConnection] = useState<LineSwitchConnection | null>(readLineSwitchConnection);
  const [state, setState] = useState<LineSwitchState | null>(null);
  const [accessUrl, setAccessUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async (next = connection) => {
    if (!next) return;
    setBusy(true);
    setError("");
    try {
      setState(await fetchLineSwitchState(next));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }, [connection]);

  useEffect(() => { void refresh(); }, [refresh]);

  const pair = async () => {
    let next: LineSwitchConnection;
    try {
      next = parseLineSwitchAccessUrl(accessUrl);
    } catch {
      setError(t("serverSetup.urlInvalid"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const snapshot = await fetchLineSwitchState(next);
      writeLineSwitchConnection(next);
      setConnection(next);
      setState(snapshot);
      setAccessUrl("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const switchTo = async (target: string) => {
    if (!connection || !state) return;
    setBusy(true);
    setError("");
    try {
      const response = await requestLineSwitch(connection, target);
      const nextUrl = targetServerUrl(state, target, connection, response.target_url);
      if (!nextUrl) throw new Error(t("serverSetup.urlInvalid"));
      if (Capacitor.isNativePlatform()) {
        writeNativeServerUrl(nextUrl);
        clearStoredToken();
        window.location.reload();
        return;
      }
      window.location.assign(nextUrl);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setBusy(false);
    }
  };

  const forget = () => {
    clearLineSwitchConnection();
    setConnection(null);
    setState(null);
    setError("");
  };

  return (
    <section className={`independent-line-switch${busy ? " is-busy" : ""}`} aria-busy={busy}>
      <div className="independent-line-switch-header">
        <strong>{t("switchControl.line")}</strong>
        {connection && (
          <button type="button" className="independent-line-switch-forget" onClick={forget}>
            {t("backendUnavailable.changeServer")}
          </button>
        )}
      </div>
      {!connection ? (
        <div className="independent-line-switch-pair">
          <label htmlFor="line-switch-access">{t("serverSetup.urlLabel")}</label>
          <input
            id="line-switch-access"
            value={accessUrl}
            onChange={(event) => setAccessUrl(event.target.value)}
            placeholder="http://host:18768/#access-key"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
          <button type="button" disabled={busy || !accessUrl.trim()} onClick={() => void pair()}>
            {busy ? t("serverSetup.testing") : t("serverSetup.connect")}
          </button>
        </div>
      ) : (
        <div className="independent-line-switch-lines">
          {state && Object.keys(state.lines).map((line) => {
            const active = line === state.active_line;
            return (
              <button
                key={line}
                type="button"
                className={active ? "active" : ""}
                disabled={busy || active || Boolean(state.incompatible?.[line])}
                onClick={() => void switchTo(line)}
              >
                {active ? `${line} — ${t("switchControl.active")}` : `${t("switchControl.switchTo")} ${line}`}
              </button>
            );
          })}
          {!state && (
            <button type="button" disabled={busy} onClick={() => void refresh()}>
              {t("backendUnavailable.retry")}
            </button>
          )}
        </div>
      )}
      {error && <div className="login-error" role="alert">{error}</div>}
    </section>
  );
}
