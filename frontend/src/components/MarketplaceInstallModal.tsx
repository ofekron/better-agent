import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { extensionPermissionTranslationKey } from "./extensionPermissions";
import { MarketplaceConfirmationModal } from "./MarketplaceConfirmationModal";

type PermissionValue = boolean | "optional" | string[];

export interface MarketplaceInstallManifest {
  id: string;
  name: string;
  version: string;
  permissions?: Record<string, PermissionValue>;
  entrypoints?: {
    mcp?: Array<{ name?: string; label?: string; execution?: "subprocess" | "warm_pool" }>;
  };
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
  const warmPoolServers = useMemo(
    () => (manifest.entrypoints?.mcp ?? [])
      .filter((server) => server.execution === "warm_pool")
      .map((server) => server.label || server.name)
      .filter((name): name is string => Boolean(name)),
    [manifest.entrypoints?.mcp],
  );
  return (
    <MarketplaceConfirmationModal
      open={open}
      title={manifest.name}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
          <div>
            <strong>{t("settings.extensionsPermissions")}</strong>
            <p style={{ color: "var(--text-secondary)" }}>{t("settings.extensionsPermissionsHelp")}</p>
          </div>
          {warmPoolServers.length > 0 && (
            <div className="extension-ui-settings-permission">
              <div className="extension-ui-settings-permission-risk">
                {t("settings.extensionsWarmPoolNotice", { tools: warmPoolServers.join(", ") })}
              </div>
            </div>
          )}
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
    </MarketplaceConfirmationModal>
  );
}
