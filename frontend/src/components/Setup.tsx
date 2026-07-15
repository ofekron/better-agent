import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { setStoredToken } from "../bearerAuth";
import { runThreeStateSync } from "../progress/store";

interface Props {
  /** Called after credentials are created + the session is established.
   * Parent re-runs the auth check and swaps in the workspace. */
  onComplete: () => void;
}

/** First-run setup form — shown only when the backend reports
 * `needs_setup` (no credentials configured yet). Posts username +
 * password to /api/auth/setup, which writes them and logs the user in.
 * The cross-platform equivalent of run.sh's terminal prompt and the
 * desktop app's native setup dialog. */
export function Setup({ onComplete }: Props) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { result: res } = await runThreeStateSync({
        operationId: "auth:setup",
        action: t("setup.submit", "Create account"),
        reconcile: onComplete,
        isAcknowledged: (response) => response.ok || response.status === 409,
        mutate: async () => {
          const response = await fetch(`${API}/api/auth/setup`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ username, password }),
          });
          if (!response.ok && response.status !== 409) {
            const detail = await response.json().catch(() => null);
            throw new Error(detail?.detail || t("setup.unknownError", { status: response.status }));
          }
          return response;
        },
      });
      if (res.ok) {
        // Capture the bearer token (native) — browsers ignore it.
        try {
          const body = await res.json();
          if (body?.token) setStoredToken(body.token);
        } catch {
          /* cookie-only browser path is fine */
        }
        onComplete();
        return;
      }
      if (res.status === 409) {
        // Already configured (e.g. another tab finished setup) — bounce
        // to the normal auth flow.
        onComplete();
        return;
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("setup.networkError", "Network error — is the backend running?"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={onSubmit}>
        <h1 className="login-title">{t("app.title")}</h1>
        <p className="login-subtitle">
          {t("setup.subtitle", "Welcome — create your login to get started.")}
        </p>
        <label className="login-field">
          <span>{t("login.usernameLabel", "Username")}</span>
          <input
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            required
          />
        </label>
        <label className="login-field">
          <span>{t("login.passwordLabel", "Password")}</span>
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && <div className="login-error" role="alert">{error}</div>}
        <button
          type="submit"
          className="login-submit"
          disabled={busy || !username || !password}
        >
          {busy
            ? t("setup.saving", "Creating…")
            : t("setup.submit", "Create account")}
        </button>
      </form>
    </div>
  );
}
