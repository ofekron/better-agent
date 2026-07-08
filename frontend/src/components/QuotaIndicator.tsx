import { useTranslation } from "react-i18next";
import type { QuotaLevel, QuotaSummary } from "../utils/quotaStatus";

const LEVEL_COLOR: Record<QuotaLevel, string> = {
  ok: "#3cb46e",
  warn: "#e0a933",
  critical: "#d9534f",
};

interface Props {
  summary: QuotaSummary | null;
  /** Compact mode renders just "dot + N% left" for tight rows. */
  compact?: boolean;
}

/** Shows remaining quota for a provider next to its name. Colors match the
 * usage-gauge (green < 70% used, yellow 70-89%, red 90%+). Renders nothing
 * when there is no usage data (unsupported provider, offline, etc.) so the
 * host row keeps its normal layout. */
export function QuotaIndicator({ summary, compact }: Props) {
  const { t } = useTranslation();
  if (!summary) return null;
  const color = LEVEL_COLOR[summary.level];
  const title = compact
    ? t("quota.rowTitle", {
        remaining: summary.remainingPercent,
        window: summary.windowLabel,
        defaultValue: "{{window}}: {{remaining}}% remaining",
      })
    : undefined;
  return (
    <span
      className={`quota-indicator quota-${summary.level}`}
      title={title}
      style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}
    >
      <span
        className="quota-indicator-dot"
        style={{
          width: "8px",
          height: "8px",
          borderRadius: "50%",
          background: color,
          flexShrink: 0,
        }}
      />
      <span style={{ color: "var(--text-secondary)" }}>
        {t("quota.remaining", {
          percent: summary.remainingPercent,
          defaultValue: "{{percent}}% left",
        })}
      </span>
      {!compact && (
        <span style={{ color: "var(--text-tertiary, var(--text-secondary))", fontSize: "0.85em" }}>
          {summary.windowLabel}
        </span>
      )}
    </span>
  );
}
