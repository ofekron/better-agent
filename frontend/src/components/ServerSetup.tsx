import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DEFAULT_BACKEND_PORT, normalizeServerUrl, writeNativeServerUrl } from "../nativeServerConfig";
import { SERVER_CANDIDATES } from "../serverCandidates.generated";
import { OpenRecoveryAppButton } from "./OpenRecoveryAppButton";

interface Props {
  onConfigured: () => void;
  /** Pre-fill the input with this URL instead of the top detected
   * candidate. Used by the "change server" flow so the user starts from
   * the server they are currently connected to. */
  initialUrl?: string;
}

export function ServerSetup({ onConfigured, initialUrl }: Props) {
  const { t } = useTranslation();
  // First run: pre-fill with the highest-ranked candidate the build
  // script detected on the desktop (Tailscale → LAN → other). Change
  // flow: pre-fill the currently connected server so the user edits
  // from a known value.
  const [url, setUrl] = useState(initialUrl ?? SERVER_CANDIDATES[0] ?? "");
  const [error, setError] = useState("");
  const [testing, setTesting] = useState(false);

  function handleSave(value?: string) {
    let cleaned = "";
    try {
      cleaned = normalizeServerUrl(value ?? url);
    } catch (e) {
      if (e instanceof Error && e.message === "required") {
        setError(t("serverSetup.urlRequired"));
        return;
      }
      setError(t("serverSetup.urlInvalid"));
      return;
    }
    setError("");
    setTesting(true);

    // Test connectivity before saving. /api/auth/needs_setup is one of
    // the few endpoints exempted from the auth gate, so it answers 200
    // even when the phone has no session cookie yet.
    fetch(`${cleaned}/api/auth/needs_setup`, { signal: AbortSignal.timeout(5000) })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        writeNativeServerUrl(cleaned);
        onConfigured();
      })
      .catch((e) => {
        setError(t("serverSetup.connectionFailed", { error: e.message }));
      })
      .finally(() => setTesting(false));
  }

  // Show alternates as one-tap chips if we have more than the one we
  // already pre-filled. The current selection (matching the input) is
  // visually marked but still clickable for "snap back" UX.
  const alternates = SERVER_CANDIDATES.filter((c) => c !== url);

  return (
    <div className="login-page">
      <div className="login-card">
        <h1 className="login-title">{t("serverSetup.title")}</h1>
        <p className="login-subtitle">
          {t("serverSetup.subtitle", { port: DEFAULT_BACKEND_PORT })}
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSave();
          }}
        >
          <div className="login-field">
            <label className="login-label" htmlFor="server-url">
              {t("serverSetup.urlLabel")}
            </label>
            <input
              id="server-url"
              type="text"
              className="login-input"
              placeholder="192.168.1.100"
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setError("");
              }}
              autoFocus
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              inputMode="url"
            />
          </div>
          {alternates.length > 0 && (
            <div className="server-setup-chips">
              <span className="server-setup-chips-label">
                {t("serverSetup.alternates", "Try:")}
              </span>
              {alternates.map((c) => (
                <button
                  key={c}
                  type="button"
                  className="server-setup-chip"
                  onClick={() => {
                    setUrl(c);
                    setError("");
                  }}
                >
                  {c.replace(/^https?:\/\//, "")}
                </button>
              ))}
            </div>
          )}
          {error && <div className="login-error">{error}</div>}
          <button className="login-submit" type="submit" disabled={testing}>
            {testing ? t("serverSetup.testing") : t("serverSetup.connect")}
          </button>
        </form>
        <OpenRecoveryAppButton />
      </div>
    </div>
  );
}
