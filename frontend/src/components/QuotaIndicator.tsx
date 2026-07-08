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
 * usage-gauge (green < 70% used, yellow 70-89%, red 90%+). Renders nothing
 * when there is no usage data (unsupported provider, offline, etc.) so the
 * host row keeps its normal layout. */
export function QuotaIndicator({ summary }: Props) {
  const { t } = useTranslation();
  if (!summary) return null;
  return (
    <span
      className={`quota-indicator quota-${summary.level}`}
      style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
      title={t("quota.rowTitle", {
        remaining: summary.remainingPercent,
        window: summary.windowLabel,
        defaultValue: "{{window}}: {{remaining}}% remaining",
      })}
    >
      <span
        className="quota-indicator-dot"
        style={{
          width: "8px",
          height: "8px",
          borderRadius: "50%",
          background: LEVEL_COLOR[summary.level],
          flexShrink: 0,
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
    </span>
  );
}
