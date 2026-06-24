import { useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type { RearrangerStats, TokenUsage as TokenUsageType } from "../types";

interface Props {
  usage?: TokenUsageType | null;
  /** Last turn's token usage (not cumulative) — used for context fill bar. */
  usageLast?: TokenUsageType | null;
  rearrangerStats?: RearrangerStats | null;
  connected: boolean;
  contextWindow?: number | null;
  /** When true (mobile), the block renders collapsed with an expand toggle. */
  collapsible?: boolean;
}

function formatNum(n: number): string {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return n.toString();
}

function estimateCost(usage: TokenUsageType): number {
  // Approximate costs per 1M tokens (Sonnet 4.6 pricing)
  const inputCost = 3; // $3/MTok
  const outputCost = 15; // $15/MTok
  const cacheReadCost = 0.3; // $0.30/MTok
  const cacheCreateCost = 3.75; // $3.75/MTok

  return (
    (usage.input_tokens * inputCost) / 1_000_000 +
    (usage.output_tokens * outputCost) / 1_000_000 +
    ((usage.cache_read_input_tokens ?? 0) * cacheReadCost) / 1_000_000 +
    ((usage.cache_creation_input_tokens ?? 0) * cacheCreateCost) / 1_000_000
  );
}

function formatCost(cost: number): string {
  if (cost < 0.01) return "<$0.01";
  return `$${cost.toFixed(2)}`;
}

/** Subtract `b` from `a` field-by-field, clamped at zero. Used to
 * derive the "chat" group (primary + workers) from the grand total
 * minus the rearranger breakdown — the backend stores the total
 * already including rearranger, so we separate for display. */
function subtractUsage(
  a: TokenUsageType,
  b: TokenUsageType | undefined | null
): TokenUsageType {
  if (!b) return a;
  return {
    input_tokens: Math.max(0, a.input_tokens - (b.input_tokens || 0)),
    output_tokens: Math.max(0, a.output_tokens - (b.output_tokens || 0)),
    cache_creation_input_tokens: Math.max(
      0,
      (a.cache_creation_input_tokens ?? 0) - (b.cache_creation_input_tokens ?? 0)
    ),
    cache_read_input_tokens: Math.max(
      0,
      (a.cache_read_input_tokens ?? 0) - (b.cache_read_input_tokens ?? 0)
    ),
  };
}

function tokensTotal(usage: TokenUsageType): number {
  return (
    (usage.input_tokens || 0) +
    (usage.output_tokens || 0) +
    (usage.cache_creation_input_tokens ?? 0) +
    (usage.cache_read_input_tokens ?? 0)
  );
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

export function TokenUsageDisplay({ usage, usageLast, rearrangerStats, connected, contextWindow, collapsible }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const hasUsage = usage && (usage.input_tokens > 0 || usage.output_tokens > 0);
  const hasRearranger =
    !!rearrangerStats && (rearrangerStats.call_count ?? 0) > 0;

  // Group-by split: chat = total minus rearranger; rearranger is its own row.
  const rearrangerUsage = rearrangerStats?.token_usage;
  const chatUsage: TokenUsageType | null = usage
    ? subtractUsage(usage, rearrangerUsage)
    : null;

  const showContextFill =
    contextWindow && contextWindow > 0 && usageLast && hasUsage;

  const summaryCost = hasUsage && usage ? formatCost(estimateCost(usage)) : null;

  // Collapsible (mobile): a compact title row doubles as the connection
  // indicator; details are hidden until expanded. Desktop keeps the flat
  // layout with the connection label always visible.
  const header = collapsible ? (
    <button
      type="button"
      className={`token-usage-toggle${expanded ? "" : " collapsed"}`}
      onClick={() => setExpanded((e) => !e)}
      aria-expanded={expanded}
    >
      <div className={`connection-dot ${connected ? "connected" : ""}`} />
      <span className="token-usage-title">{t("tokens.stats")}</span>
      <span className="token-usage-toggle-right">
        {!expanded && summaryCost && (
          <span className="token-usage-toggle-summary">{summaryCost}</span>
        )}
        <span className="collapse-arrow">{expanded ? <Icon name="chevron-down" size={12} /> : <Icon name="chevron-right" size={12} />}</span>
      </span>
    </button>
  ) : (
    <div className="token-usage-row">
      <div className={`connection-dot ${connected ? "connected" : ""}`} />
      <span className="connection-label">
        {connected ? t("tokens.connected") : t("tokens.disconnected")}
      </span>
    </div>
  );

  const showDetails = !collapsible || expanded;

  return (
    <div className="token-usage">
      {header}
      {showDetails && showContextFill && (
        <ContextFillBar
          used={contextFillTokens(usageLast)}
          capacity={contextWindow}
        />
      )}
      {showDetails && hasUsage && usage && (
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
                <span className="token-label">{t("tokens.cache")}</span>
                <span className="token-value">
                  {formatNum(usage.cache_read_input_tokens)}
                </span>
              </div>
            )}
          </div>
          <div className="token-usage-row">
            <span className="token-cost">{formatCost(estimateCost(usage))}</span>
          </div>
        </>
      )}
      {showDetails && hasRearranger && rearrangerStats && (
        <div className="token-breakdown">
          <div className="token-breakdown-title">{t("tokens.breakdown")}</div>
          {chatUsage && tokensTotal(chatUsage) > 0 && (
            <div className="token-breakdown-row">
              <span className="token-breakdown-label">{t("tokens.chat")}</span>
              <span className="token-breakdown-value">
                {formatNum(tokensTotal(chatUsage))} tok
              </span>
              <span className="token-breakdown-cost">
                {formatCost(estimateCost(chatUsage))}
              </span>
            </div>
          )}
          <div className="token-breakdown-row">
            <span className="token-breakdown-label">
              {t("tokens.rearranger")}
              <span className="token-breakdown-meta">
                {" "}· {rearrangerStats.call_count}{" "}
                {rearrangerStats.call_count === 1 ? t("tokens.call") : t("tokens.calls")}
              </span>
            </span>
            <span className="token-breakdown-value">
              {formatNum(tokensTotal(rearrangerStats.token_usage))} tok
            </span>
            <span className="token-breakdown-cost">
              {rearrangerStats.total_cost_usd > 0
                ? formatCost(rearrangerStats.total_cost_usd)
                : formatCost(estimateCost(rearrangerStats.token_usage))}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
