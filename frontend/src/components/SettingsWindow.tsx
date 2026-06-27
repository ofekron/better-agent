import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import { SettingsPage } from "./SettingsPage";
import { API } from "../api";
import { useBuiltinExtensionFlags } from "../hooks/useBuiltinExtensionFlags";
import { useRefreshApp } from "../hooks/useRefreshApp";
import { openProviderConfigSyncPage } from "../lib/providerConfigSyncRoute";

/** Dedicated chrome-less window that renders the Settings page on its own,
 *  opened via `window.open(?settings_window=1)` from the main app. Reuses the
 *  shared builtin-extension and refresh-app hooks so behavior matches the
 *  in-app settings route exactly; closing the page closes the window. */
export function SettingsWindow() {
  const { t } = useTranslation();
  const builtinExtensions = useBuiltinExtensionFlags("authed");
  const { restarting, restartError, dismissRestartError, openRefreshModal, refreshModal } =
    useRefreshApp();

  useEffect(() => {
    const previousTitle = document.title;
    document.title = t("app.settingsButtonTitle");
    return () => {
      document.title = previousTitle;
    };
  }, [t]);

  return (
    <>
      {restartError && (
        <div className="restart-error-banner" role="alert">
          <span className="restart-error-banner-text">{restartError}</span>
          <button
            className="restart-error-banner-close"
            onClick={dismissRestartError}
            aria-label={t("startup_tasks.dismiss")}
            title={t("startup_tasks.dismiss")}
          >
            <Icon name="x" size={18} />
          </button>
        </div>
      )}
      <SettingsPage
        onClose={() => window.close()}
        onRefreshApp={openRefreshModal}
        refreshAppDisabled={restarting}
        teamEnabled={builtinExtensions.team}
        credentialBrokerEnabled={builtinExtensions.credentialBroker}
        providerConfigSyncEnabled={builtinExtensions.providerConfigSync}
        onOpenProviderConfigSync={
          builtinExtensions.providerConfigSync
            ? () => openProviderConfigSyncPage(API)
            : undefined
        }
      />
      {refreshModal}
    </>
  );
}
