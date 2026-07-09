import { useTranslation } from "react-i18next";
import type { QuotaLevel, QuotaSummary } from "../utils/quotaStatus";

const LEVEL_COLOR: Record<QuotaLevel, string> = {
  ok: "#3cb46e",
  warn: "#e0a933",
  critical: "#d9534f",
};

interface Props {
  summary: QuotaSummary | null;
}

/** Shows remaining quota for a provider next to its name. Colors match the
 * usage-gauge (green < 70% used, yellow 70-89%, red 90%+). A stale reading
 * (last-good snapshot re-served while the live fetch fails) dims the row and
 * says so, instead of disappearing. Renders nothing only when there is no
 * usage data at all (unsupported provider, never fetched). */
export function QuotaIndicator({ summary }: Props) {
  const { t } = useTranslation();
  if (!summary) return null;
  const title = summary.stale
    ? t("quota.rowTitleStale", {
        remaining: summary.remainingPercent,
        window: summary.windowLabel,
        error: summary.error ?? "",
        defaultValue:
          "{{window}}: {{remaining}}% remaining (last known — refresh failing: {{error}})",
      })
    : t("quota.rowTitle", {
        remaining: summary.remainingPercent,
        window: summary.windowLabel,
        defaultValue: "{{window}}: {{remaining}}% remaining",
      });
  return (
    <span
      className={`quota-indicator quota-${summary.level}${summary.stale ? " quota-stale" : ""}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "5px",
        opacity: summary.stale ? 0.6 : 1,
        transition: "opacity 0.3s ease",
      }}
      title={title}
    >
      <span
        className="quota-indicator-dot"
        style={{
          width: "8px",
          height: "8px",
          borderRadius: "50%",
          background: LEVEL_COLOR[summary.level],
          flexShrink: 0,
          transition: "background-color 0.3s ease",
        }}
      />
      <span style={{ color: "var(--text-secondary)" }}>
        {t("quota.remaining", {
          percent: summary.remainingPercent,
          defaultValue: "{{percent}}% left",
        })}
      </span>
      <span style={{ color: "var(--text-tertiary, var(--text-secondary))", fontSize: "0.85em" }}>
        {summary.windowLabel}
      </span>
      {summary.stale && (
        <span style={{ color: "var(--text-tertiary, var(--text-secondary))", fontSize: "0.85em" }}>
          {t("quota.stale", { defaultValue: "stale" })}
        </span>
      )}
    </span>
  );
}
