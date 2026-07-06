import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DEFAULT_BACKEND_PORT } from "../backendPort";
import { SERVER_CANDIDATES } from "../serverCandidates.generated";

interface Props {
  onConfigured: () => void;
}

export function normalizeServerUrl(value: string): string {
  const raw = value.trim().replace(/\/+$/, "");
  if (!raw) {
    throw new Error("required");
  }
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
  const parsed = new URL(withScheme);
  if (!parsed.port && parsed.protocol === "http:") {
    parsed.port = DEFAULT_BACKEND_PORT;
  }
  return `${parsed.protocol}//${parsed.host}`;
}

export function ServerSetup({ onConfigured }: Props) {
  const { t } = useTranslation();
  // Pre-fill with the highest-ranked candidate the build script detected
  // on the desktop (Tailscale → LAN → other). The user can edit or pick a
  // different chip below.
  const [url, setUrl] = useState(SERVER_CANDIDATES[0] ?? "");
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
        localStorage.setItem("better_agent_server_url", cleaned);
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
      </div>
    </div>
  );
}
