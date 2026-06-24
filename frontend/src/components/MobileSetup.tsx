import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { scaledFontSize } from "../utils/typography";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function MobileSetup({ open, onClose }: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<{ android: boolean; ios: boolean } | null>(null);
  const [androidQr, setAndroidQr] = useState("");
  const [iosQr, setIosQr] = useState("");

  // The backend reports a LAN-reachable base URL (its own IP), since the
  // QR must point at an address a phone can reach — window.location.host
  // is usually localhost, which a phone can't. Fall back to it only if the
  // backend didn't supply one.
  const [serverUrl, setServerUrl] = useState(
    API || `${window.location.protocol}//${window.location.host}`
  );

  useEffect(() => {
    if (!open) return;
    fetch(`${API}/api/mobile/status`)
      .then((r) => r.json())
      .then((d: { android: boolean; ios: boolean; server_url?: string }) => {
        setStatus({ android: d.android, ios: d.ios });
        if (d.server_url) setServerUrl(d.server_url);
      })
      .catch(() => setStatus({ android: false, ios: false }));
  }, [open]);

  const generateQr = useCallback(async (data: string): Promise<string> => {
    const QRCode = await import("qrcode");
    return QRCode.toDataURL(data, { width: 180, margin: 1, color: { dark: "#000", light: "#fff" } });
  }, []);

  useEffect(() => {
    if (!open || !status) return;
    // Point at the SPA download route (NOT the gated /api endpoint
    // directly) so an unauthenticated phone gets the login page first,
    // then the download auto-starts. Query param (not a path segment) so
    // the SPA's relative asset URLs still resolve.
    const androidUrl = `${serverUrl}/?download=android`;
    const iosUrl = `${serverUrl}/?download=ios`;

    if (status.android) generateQr(androidUrl).then(setAndroidQr);
    if (status.ios) generateQr(iosUrl).then(setIosQr);
  }, [open, status, serverUrl, generateQr]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 480 }}>
        <div className="modal-header">
          <h2>{t("mobileSetup.title")}</h2>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body">
          <p className="setup-mode-desc">{t("mobileSetup.subtitle")}</p>

          <div style={{ marginBottom: 24 }}>
            <h3 style={{ marginBottom: 8 }}>Android</h3>
            {status?.android && androidQr ? (
              <div style={{ textAlign: "center" }}>
                <img src={androidQr} alt="Android download QR" width={180} height={180} style={{ borderRadius: 8 }} />
                <p style={{ marginTop: 8, fontSize: scaledFontSize(13), color: "var(--text-muted)" }}>
                  {t("mobileSetup.scanToDownload")}
                </p>
              </div>
            ) : (
              <p style={{ color: "var(--text-muted)", fontSize: scaledFontSize(13) }}>{t("mobileSetup.noAndroid")}</p>
            )}
          </div>

          <div>
            <h3 style={{ marginBottom: 8 }}>iOS</h3>
            {status?.ios && iosQr ? (
              <div style={{ textAlign: "center" }}>
                <img src={iosQr} alt="iOS download QR" width={180} height={180} style={{ borderRadius: 8 }} />
                <p style={{ marginTop: 8, fontSize: scaledFontSize(13), color: "var(--text-muted)" }}>
                  {t("mobileSetup.scanToDownload")}
                </p>
              </div>
            ) : (
              <p style={{ color: "var(--text-muted)", fontSize: scaledFontSize(13) }}>{t("mobileSetup.noIos")}</p>
            )}
          </div>

          <p style={{ marginTop: 16, fontSize: scaledFontSize(12), color: "var(--text-muted)" }}>
            {t("mobileSetup.hint")}
          </p>
        </div>
      </div>
    </div>
  );
}
