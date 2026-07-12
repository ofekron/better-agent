import { useTranslation } from "react-i18next";
import {
  quotaLevel,
  quotaResetText,
  type QuotaLevel,
  type QuotaProviderStatus,
  type QuotaWindow,
} from "../utils/quotaStatus";

const LEVEL_COLOR: Record<QuotaLevel, string> = {
  ok: "#3cb46e",
  warn: "#e0a933",
  critical: "#d9534f",
};

interface Props {
  status?: QuotaProviderStatus;
}

function QuotaWindowRow({ window, stale }: { window: QuotaWindow; stale: boolean }) {
  const { t } = useTranslation();
  const usedPercent = Math.max(0, Math.min(100, Math.round(window.used_percent)));
  const remainingPercent = 100 - usedPercent;
  const level = quotaLevel(usedPercent);
  const reset = quotaResetText({
    usedPercent,
    remainingPercent,
    level,
    windowLabel: window.label,
    resetsAt: window.resets_at,
  }, t);
  const title = stale
    ? t("quota.rowTitleStale", {
        remaining: remainingPercent,
        window: window.label,
        error: "",
        defaultValue:
          "{{window}}: {{remaining}}% remaining (last known — refresh failing: {{error}})",
      })
    : t("quota.rowTitle", {
        remaining: remainingPercent,
        window: window.label,
        defaultValue: "{{window}}: {{remaining}}% remaining",
      });
  return (
    <div
      className={`quota-window quota-${level}${stale ? " quota-stale" : ""}`}
      title={title}
    >
      <span
        className="quota-indicator-dot"
        style={{ background: LEVEL_COLOR[level] }}
      />
      <span className="quota-window-label">{window.label}</span>
      <span className="quota-window-remaining">
        {t("quota.remaining", {
          percent: remainingPercent,
          defaultValue: "{{percent}}% left",
        })}
      </span>
      {reset && <span className="quota-window-meta">{reset}</span>}
      {typeof window.minutes_to_exhaustion === "number" && (
        <span className="quota-window-meta">~{Math.round(window.minutes_to_exhaustion)}m</span>
      )}
      {stale && (
        <span className="quota-window-meta">
          {t("quota.stale", { defaultValue: "stale" })}
        </span>
      )}
    </div>
  );
}

/** Complete quota projection for a provider card. Every reported time/model
 * window stays visible; providers without usable data remain explicit. */
export function QuotaIndicator({ status }: Props) {
  const { t } = useTranslation();
  const windows = status?.supported !== false && (!status?.error || status.stale)
    ? (status?.windows ?? []).filter((window) => Number.isFinite(window.used_percent))
    : [];
  if (!windows.length) {
    return <div className="quota-empty">{t("tokens.noUsage")}</div>;
  }
  return (
    <div className="quota-indicator" aria-label={status?.label}>
      {windows.map((window) => (
        <QuotaWindowRow key={window.key} window={window} stale={status?.stale === true} />
      ))}
    </div>
  );
}
