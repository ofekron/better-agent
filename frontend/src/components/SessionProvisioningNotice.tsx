import { useTranslation } from "react-i18next";

import type { ForkProvisioning } from "src/types";

import "../styles/sessionProvisioning.css";

/**
 * Truthful state for a session that exists but is still waiting for the
 * provisioned base it forks from. The composer stays usable on purpose —
 * anything typed here is queued by the backend and handed over the moment
 * provisioning completes — so this explains why a prompt sits queued
 * rather than blocking the user out of the session.
 *
 * Indeterminate by design: the warm has no meaningful progress units, and
 * a fake percentage would be worse than an honest shimmer.
 */
export function SessionProvisioningNotice({
  provisioning,
}: {
  provisioning: ForkProvisioning | null | undefined;
}) {
  const { t } = useTranslation();
  if (!provisioning) return null;

  const failed = provisioning.state === "failed";
  return (
    <div
      className={`session-provisioning${failed ? " is-failed" : ""}`}
      role="status"
      aria-live="polite"
    >
      <span className="session-provisioning-mark" aria-hidden="true" />
      <div className="session-provisioning-text">
        <span className="session-provisioning-title">
          {failed
            ? t("session.provisioningFailed", "Session setup failed")
            : t("session.provisioning", "Preparing this session…")}
        </span>
        <span className="session-provisioning-detail">
          {failed
            ? provisioning.error
              ? t("session.provisioningFailedDetail", {
                  defaultValue: "{{error}} — send a message to try again.",
                  error: provisioning.error,
                })
              : t(
                  "session.provisioningFailedRetry",
                  "Send a message to try again.",
                )
            : t(
                "session.provisioningDetail",
                "Anything you send now is queued and delivered as soon as it is ready.",
              )}
        </span>
      </div>
    </div>
  );
}
