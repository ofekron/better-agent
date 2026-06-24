import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";

/** Shown when the URL carries `?download=android|ios` AND the user is
 * authenticated. The mobile QR points here (not straight at the gated
 * /api/download endpoint) so an unauthenticated phone lands on <Login />
 * first; once logged in, App re-renders into this component, which
 * auto-starts the (now authenticated, cookie-bearing) file download. */
export function DownloadRedirect({ platform }: { platform: "android" | "ios" }) {
  const { t } = useTranslation();
  const url = `${API}/api/download/${platform}`;

  useEffect(() => {
    // We're authed, so the better_agent_session cookie is set and the gated endpoint
    // will serve the file. Navigating to it triggers the browser's download
    // (Content-Disposition: attachment) without leaving this page.
    const id = setTimeout(() => {
      window.location.href = url;
    }, 500);
    return () => clearTimeout(id);
  }, [url]);

  return (
    <div className="login-shell">
      <div className="login-card" style={{ textAlign: "center" }}>
        <h1 className="login-title">{t("download.title", "Downloading…")}</h1>
        <p className="login-subtitle">
          {t("download.subtitle", "Your download should start automatically.")}
        </p>
        <a
          className="login-submit"
          href={url}
          style={{ textDecoration: "none", display: "inline-block" }}
        >
          {t("download.manual", "Tap here if it doesn’t start")}
        </a>
      </div>
    </div>
  );
}
