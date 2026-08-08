import { useTranslation } from "react-i18next";
import Icon, { type IconName } from "./Icon";
import type { BackgroundWorkItem, BackgroundWorkStatus } from "../types";

/** One row in the background work stack.
 *
 * Owner-supplied text (`label`, `detail`) is rendered as text under
 * provenance chrome naming the owning extension, so an extension can never
 * dress its own string up as a core message.
 *
 * Progress is determinate only when the backend supplies a `total`. With no
 * total the bar is explicitly indeterminate and only the count is shown — a
 * percentage is never inferred.
 */

const STATUS_ICON: Record<BackgroundWorkStatus, IconName> = {
  pending: "clock",
  running: "refresh",
  succeeded: "check",
  failed: "warning",
  cancelled: "x-circle",
  unknown: "info",
};

export function BackgroundWorkItemCard({
  item,
  stale,
  onDismiss,
}: {
  item: BackgroundWorkItem;
  stale: boolean;
  onDismiss: (id: string) => void;
}) {
  const { t } = useTranslation();

  // Core rows carry an i18n key; extension rows carry only their own text,
  // which is never treated as a key.
  const heading = item.title_key
    ? t(item.title_key, {
      ...item.title_params,
      defaultValue: item.label,
    })
    : item.label;

  const determinate = item.progress && item.progress.total !== null;
  const pct = determinate
    ? Math.min(100, Math.round((item.progress!.completed / item.progress!.total!) * 100))
    : null;

  return (
    <li className={`background-work-row background-work-row-${item.status}`}>
      <span className="background-work-row-icon" aria-hidden>
        <Icon name={STATUS_ICON[item.status]} size={12} />
      </span>
      <div className="background-work-row-body">
        {item.owner_kind === "extension" ? (
          <span className="background-work-row-origin">
            {t("backgroundWork.fromExtension", { extension: item.owner_id })}
          </span>
        ) : null}
        <span className="background-work-row-label">{heading}</span>
        {item.phase ? (
          <span className="background-work-row-phase">{item.phase}</span>
        ) : null}
        {item.detail ? (
          <span className="background-work-row-detail">{item.detail}</span>
        ) : null}

        {item.status === "running" || item.status === "pending" ? (
          <div
            className={`background-work-progress${determinate ? "" : " background-work-progress-indeterminate"}`}
            role="progressbar"
            aria-valuenow={determinate ? item.progress!.completed : undefined}
            aria-valuemax={determinate ? item.progress!.total! : undefined}
            aria-valuetext={determinate ? undefined : t("backgroundWork.inProgress")}
          >
            <div
              className="background-work-progress-fill"
              style={determinate ? { width: `${pct}%` } : undefined}
            />
          </div>
        ) : null}

        {determinate ? (
          <span className="background-work-row-count">
            {t("backgroundWork.count", {
              completed: item.progress!.completed,
              total: item.progress!.total,
              unit: item.progress!.unit,
            })}
          </span>
        ) : null}

        {item.status === "unknown" ? (
          <span className="background-work-row-note">
            {t("backgroundWork.stateUnknown")}
          </span>
        ) : null}
        {stale ? (
          <span className="background-work-row-note">
            {t("backgroundWork.disconnected")}
          </span>
        ) : null}
        {item.error ? (
          <span className="background-work-row-error">{item.error}</span>
        ) : null}
      </div>
      {item.dismissible ? (
        <button
          className="background-work-row-close"
          onClick={() => onDismiss(item.id)}
          aria-label={t("backgroundWork.dismiss")}
          title={t("backgroundWork.dismiss")}
        >
          ×
        </button>
      ) : null}
    </li>
  );
}
