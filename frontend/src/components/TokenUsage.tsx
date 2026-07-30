import { useTranslation } from "react-i18next";
import type { TokenUsage as TokenUsageType } from "../types";
import {
  formatCost,
  formatRate,
  hasCacheWriteBreakdown,
  usageCost,
  type PriceEntry,
} from "../utils/pricing";

interface Props {
  usage?: TokenUsageType | null;
  /** Last turn's token usage (not cumulative) — used for context fill bar. */
  usageLast?: TokenUsageType | null;
  contextWindow?: number | null;
  /** Published price for the provider x model that produced this usage,
   * from the Usage extension's daily-refreshed table. Undefined while it
   * is still loading; `available: false` when the model has no published
   * price — both render as an explicit unknown, never as a fake number. */
  price?: PriceEntry;
}

function formatNum(n: number): string {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return n.toString();
}

/** Current context fill = latest turn's total input tokens.
 * input_tokens + cache_read + cache_creation = everything the model saw. */
function contextFillTokens(usage: TokenUsageType): number {
  return (
    (usage.input_tokens || 0) +
    (usage.cache_read_input_tokens ?? 0) +
    (usage.cache_creation_input_tokens ?? 0)
  );
}

function ContextFillBar({ used, capacity }: { used: number; capacity: number }) {
  const pct = Math.min(100, (used / capacity) * 100);
  let colorClass = "context-fill-green";
  if (pct > 80) colorClass = "context-fill-red";
  else if (pct > 60) colorClass = "context-fill-yellow";

  return (
    <div className="context-fill">
      <div className="context-fill-bar">
        <div
          className={`context-fill-track ${colorClass}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="context-fill-label">
        {formatNum(used)} / {formatNum(capacity)}
      </span>
    </div>
  );
}

/** Cost of this usage at the model's published rates. Three honest states:
 *  a real number, "still loading the price table", or "this model has no
 *  published price" — never a stand-in figure. */
function CostRow({
  usage,
  price,
}: {
  usage: TokenUsageType;
  price?: PriceEntry;
}) {
  const { t } = useTranslation();
  const cost = usageCost(usage, price);

  if (cost === null) {
    const pending = price === undefined;
    return (
      <div className="token-usage-row">
        <span className="token-cost token-cost-unknown" aria-live="polite">
          {pending
            ? t("tokens.costLoading", { defaultValue: "Pricing…" })
            : t("tokens.costUnavailable", { defaultValue: "No published price" })}
        </span>
      </div>
    );
  }

  const currency = price?.currency || "USD";
  const rates = price?.cost;
  const rateHint = rates
    ? t("tokens.rateHint", {
        input: formatRate(rates.input, currency),
        output: formatRate(rates.output, currency),
        model: price?.catalog_model ?? "",
        defaultValue: "{{model}} · {{input}} in / {{output}} out per 1M tokens",
      })
    : undefined;

  return (
    <div className="token-usage-row">
      <span className="token-cost token-cost-resolved" title={rateHint}>
        {formatCost(cost.amount, currency)}
      </span>
      {cost.partial && (
        <span className="token-cost-note">
          {t("tokens.costPartial", { defaultValue: "partial — some rates unpublished" })}
        </span>
      )}
      {price?.basis === "underlying_vendor_list" && (
        <span className="token-cost-note">
          {t("tokens.costListPrice", { defaultValue: "model list price" })}
        </span>
      )}
    </div>
  );
}

export function TokenUsageDisplay({
  usage,
  usageLast,
  contextWindow,
  price,
}: Props) {
  const { t } = useTranslation();
  const hasUsage = usage && (usage.input_tokens > 0 || usage.output_tokens > 0);

  const showContextFill =
    contextWindow && contextWindow > 0 && usageLast && hasUsage;

  return (
    <div className="token-usage">
      {showContextFill && (
        <ContextFillBar
          used={contextFillTokens(usageLast)}
          capacity={contextWindow}
        />
      )}
      {hasUsage && usage && (
        <>
          <div className="token-usage-row">
            <div className="token-stat">
              <span className="token-label">{t("tokens.input")}</span>
              <span className="token-value">{formatNum(usage.input_tokens)}</span>
            </div>
            <div className="token-stat">
              <span className="token-label">{t("tokens.output")}</span>
              <span className="token-value">{formatNum(usage.output_tokens)}</span>
            </div>
            {(usage.cache_read_input_tokens ?? 0) > 0 && (
              <div className="token-stat">
                <span className="token-label">{t("tokens.cacheRead")}</span>
                <span className="token-value">
                  {formatNum(usage.cache_read_input_tokens)}
                </span>
              </div>
            )}
            {hasCacheWriteBreakdown(usage) ? (
              <>
                <div className="token-stat">
                  <span className="token-label">{t("tokens.cacheWrite5m")}</span>
                  <span className="token-value">
                    {formatNum(usage.cache_creation_5m_tokens ?? 0)}
                  </span>
                </div>
                <div className="token-stat">
                  <span className="token-label">{t("tokens.cacheWrite1h")}</span>
                  <span className="token-value">
                    {formatNum(usage.cache_creation_1h_tokens ?? 0)}
                  </span>
                </div>
              </>
            ) : (
              (usage.cache_creation_input_tokens ?? 0) > 0 && (
                <div className="token-stat">
                  <span className="token-label">{t("tokens.cacheWrite")}</span>
                  <span className="token-value">
                    {formatNum(usage.cache_creation_input_tokens)}
                  </span>
                </div>
              )
            )}
          </div>
          <CostRow usage={usage} price={price} />
        </>
      )}
      {!hasUsage && (
        <div className="token-usage-row token-usage-empty">
          {t("tokens.noUsage")}
        </div>
      )}
    </div>
  );
}
