import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import type { DesktopInstallOffer } from "../hooks/useDesktopInstallOffer";

interface Props {
  offer: DesktopInstallOffer;
  onDismiss: () => void;
}

export function DesktopInstallPrompt({ offer, onDismiss }: Props) {
  const { t } = useTranslation();

  const install = () => {
    onDismiss();
    window.location.href = offer.url;
  };

  return (
    <div className="desktop-install-prompt" role="status">
      <div className="desktop-install-icon" aria-hidden>
        <Icon name="archive" size={18} />
      </div>
      <div className="desktop-install-copy">
        <div className="desktop-install-title">
          {t("desktopInstall.title", { platform: offer.label })}
        </div>
        <div className="desktop-install-body">
          {t("desktopInstall.body")}
        </div>
      </div>
      <div className="desktop-install-actions">
        <button type="button" className="desktop-install-secondary" onClick={onDismiss}>
          {t("desktopInstall.later")}
        </button>
        <button type="button" className="desktop-install-primary" onClick={install}>
          {t("desktopInstall.install")}
        </button>
      </div>
    </div>
  );
}
