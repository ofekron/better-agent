import { useTranslation } from "react-i18next";
import { OpenRecoveryAppButton } from "./OpenRecoveryAppButton";

export function BasCompanionAppsSetting() {
  const { t } = useTranslation();
  return (
    <section className="bas-companion-setting">
      <div className="bas-companion-header">
        <div>
          <h3>{t("settings.recoveryTitle")}</h3>
          <p>{t("settings.recoverySubtitle")}</p>
        </div>
      </div>
      <OpenRecoveryAppButton className="setup-save-btn" />
    </section>
  );
}
