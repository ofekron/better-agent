import { useState, useEffect, useCallback, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { setStoredToken, setTokens } from "../bearerAuth";

interface Props {
  /** Called after a successful login. Parent re-fetches /api/auth/me
   * and swaps in the workspace, so a hard reload isn't required. */
  onSuccess: () => void;
}

/** Single-user login form. Posts username + password to
 * /api/auth/login; the backend sets the `better_agent_session` cookie on
 * success, which then gates every subsequent /api/* request and
 * the /ws/chat WebSocket.
 *
 * Plus a passwordless path for external devices:
 *   - The login screen renders a one-time QR (GET /api/auth/qr_grant,
 *     mintable only from loopback / an authed session) encoding
 *     .../?qr=<grant>. A phone scans it with its camera.
 *   - Opening .../?qr=<grant> redeems it (POST /api/auth/qr_redeem) for a
 *     short access token + rotating refresh token — no password typed,
 *     no long-lived credential on the phone. */
export function Login({ onSuccess }: Props) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [qr, setQr] = useState("");

  const doLogin = useCallback(
    async (u: string, p: string): Promise<boolean> => {
      setBusy(true);
      setError(null);
      try {
        const res = await fetch(`${API}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ username: u, password: p }),
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
          return true;
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
      return false;
    },
    [onSuccess, t]
  );

  // Scan-to-login: the phone camera opens .../?qr=<grant>. Redeem it once
  // for tokens, strip the param (one-time anyway, but keep it out of
  // history), and enter the app.
  useEffect(() => {
    const grant = new URLSearchParams(window.location.search).get("qr");
    if (!grant) return;
    window.history.replaceState(null, "", window.location.pathname);
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const res = await fetch(`${API}/api/auth/qr_redeem`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ grant }),
        });
        if (res.ok) {
          const body = await res.json();
          if (body?.access_token && body?.refresh_token) {
            setTokens(body.access_token, body.refresh_token);
          }
          onSuccess();
          return;
        }
        setError(
          res.status === 401
            ? "This QR code expired or was already used — generate a new one."
            : `Sign-in failed (${res.status}).`
        );
      } catch {
        setError(t("login.networkError"));
      } finally {
        setBusy(false);
      }
    })();
  }, [onSuccess, t]);

  // Mint + render the login QR, then re-mint before it expires so the
  // displayed code is always redeemable. 403/409 (not loopback/authed, or
  // not configured) → no QR shown, password login still works.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has("qr")) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const res = await fetch(`${API}/api/auth/qr_grant`, {
          credentials: "include",
        });
        if (!res.ok || !alive) return;
        const { login_url, expires_in } = await res.json();
        if (!login_url || !alive) return;
        const QRCode = await import("qrcode");
        const dataUrl = await QRCode.toDataURL(login_url, {
          width: 180,
          margin: 1,
          color: { dark: "#000", light: "#fff" },
        });
        if (!alive) return;
        setQr(dataUrl);
        const ms = Math.max(30, (Number(expires_in) || 300) - 30) * 1000;
        timer = setTimeout(load, ms);
      } catch {
        /* no QR — typed login still works */
      }
    };
    void load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void doLogin(username, password);
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
        {qr && (
          <div style={{ marginTop: 20, textAlign: "center" }}>
            <img
              src={qr}
              alt="One-time login QR"
              width={180}
              height={180}
              style={{ borderRadius: 8 }}
            />
            <p style={{ marginTop: 8, fontSize: 13, color: "var(--text-muted)" }}>
              Scan with a phone to sign in — one-time, expires shortly
            </p>
          </div>
        )}
      </form>
    </div>
  );
}
