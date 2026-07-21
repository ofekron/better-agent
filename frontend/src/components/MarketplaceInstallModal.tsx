import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { extensionPermissionTranslationKey } from "./extensionPermissions";

type PermissionValue = boolean | "optional" | string[];

export interface MarketplaceInstallManifest {
  id: string;
  name: string;
  version: string;
  permissions?: Record<string, PermissionValue>;
}

interface Props {
  open: boolean;
  manifest: MarketplaceInstallManifest;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function MarketplaceInstallModal({
  open,
  manifest,
  busy,
  error,
  onConfirm,
  onCancel,
}: Props) {
  const { t } = useTranslation();
  const permissions = useMemo(
    () => Object.entries(manifest.permissions ?? {})
      .filter(([, value]) => value !== false)
      .sort(([left], [right]) => left.localeCompare(right)),
    [manifest.permissions],
  );
  useBackButtonDismiss(open, busy ? () => undefined : onCancel);
  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onCancel}>
      <div className="modal-content" style={{ maxWidth: "560px" }} onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h2>{manifest.name}</h2>
          <button className="modal-close" onClick={onCancel} disabled={busy} aria-label={t("app.cancel")}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          <div>
            <strong>{t("settings.extensionsPermissions")}</strong>
            <p style={{ color: "var(--text-secondary)" }}>{t("settings.extensionsPermissionsHelp")}</p>
          </div>
          {permissions.map(([permission, value]) => (
            <div className="extension-ui-settings-permission" key={permission}>
              <div className="extension-ui-settings-permission-main">
                <div className="extension-ui-settings-permission-copy">
                  <div className="extension-ui-settings-permission-title">
                    {t(extensionPermissionTranslationKey(permission, "label"))}
                  </div>
                  <div className="extension-ui-settings-permission-risk">
                    {t(extensionPermissionTranslationKey(permission, "risk"))}
                  </div>
                  {Array.isArray(value) && value.length > 0 && (
                    <div className="extension-ui-settings-permission-scope">
                      {t("settings.extensionsPermission.scope", { scope: value.join(", ") })}
                    </div>
                  )}
                </div>
                <span className="extension-ui-settings-permission-mode">
                  {t(value === "optional"
                    ? "settings.extensionsPermissionMode.optionalOff"
                    : Array.isArray(value)
                      ? "settings.extensionsPermissionMode.scoped"
                      : "settings.extensionsPermissionMode.required")}
                </span>
              </div>
              <div className="extension-ui-settings-permission-key">{permission}</div>
            </div>
          ))}
          {error && <div className="setup-error">{error}</div>}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            {t("app.cancel")}
          </button>
          <button type="button" className="btn-primary" onClick={onConfirm} disabled={busy} autoFocus>
            {busy ? t("settings.extensionsUpdating") : t("app.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
