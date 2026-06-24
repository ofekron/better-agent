import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { setStoredToken } from "../bearerAuth";

interface Props {
  /** Called after a successful login. Parent re-fetches /api/auth/me
   * and swaps in the workspace, so a hard reload isn't required. */
  onSuccess: () => void;
}

/** Single-user login form. Posts username + password to
 * /api/auth/login; the backend sets the `better_agent_session` cookie on
 * success, which then gates every subsequent /api/* request and
 * the /ws/chat WebSocket. */
export function Login({ onSuccess }: Props) {
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
      const res = await fetch(`${API}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ username, password }),
      });
      if (res.ok) {
        // Native clients (Capacitor) need the bearer token because the
        // session cookie won't cross origins. Browsers ignore it and
        // ride the cookie. Tolerant if the body isn't JSON (defensive).
        try {
          const body = await res.json();
          if (body?.token) setStoredToken(body.token);
        } catch {
          /* no token in body → cookie-only browser path is fine */
        }
        onSuccess();
        return;
      }
      if (res.status === 429) {
        setError(t("login.tooManyAttempts"));
      } else if (res.status === 401) {
        setError(t("login.invalidCredentials"));
      } else {
        setError(t("login.unknownError", { status: res.status }));
      }
    } catch {
      setError(t("login.networkError"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={onSubmit}>
        <h1 className="login-title">{t("app.title")}</h1>
        <p className="login-subtitle">{t("login.subtitle")}</p>
        <label className="login-field">
          <span>{t("login.usernameLabel")}</span>
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
          <span>{t("login.passwordLabel")}</span>
          <input
            type="password"
            autoComplete="current-password"
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
          {busy ? t("login.signingIn") : t("login.signIn")}
        </button>
      </form>
    </div>
  );
}
