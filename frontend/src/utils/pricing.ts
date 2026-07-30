import { PUBLIC_EXTENSION_IDS } from "src/extensionIds";
import type { TokenUsage } from "../types";

// Per-model token pricing served by the Usage extension, which refreshes
// its table from the upstream catalog once a day. Pure wire types + cost
// math; the fetch lives in usePricing. Resolution from a provider/model
// to a catalog entry is deliberately NOT mirrored here — the extension is
// its single implementation.

export const PRICING_PATH = `/api/extensions/${PUBLIC_EXTENSION_IDS.usage}/backend/pricing`;

export const pricingUrl = (apiBase: string): string => `${apiBase}${PRICING_PATH}`;

/** USD per 1M tokens. Fields the catalog does not publish are absent —
 * absent means unknown, never zero. */
export interface ModelCost {
  input: number;
  output: number;
  cache_read?: number;
  cache_write?: number;
}

export interface PriceEntry {
  available: boolean;
  /** `provider_catalog` = this provider's own published rate.
   * `underlying_vendor_list` = the list price of the model the provider
   * resells, so the number is the model's price, not necessarily the bill. */
  basis?: "provider_catalog" | "underlying_vendor_list";
  catalog_provider?: string;
  catalog_model?: string;
  currency?: string;
  cost?: ModelCost;
  /** Why no price: model_not_priced | provider_pricing_unpublished |
   * catalog_unavailable. */
  reason?: string;
}

export interface PricingMeta {
  source: string;
  unit: string;
  currency: string;
  refresh_interval_seconds: number;
  fetched_at: number | null;
  age_seconds: number | null;
  /** No table yet — the extension is still fetching its first one. */
  loading: boolean;
  /** Serving a table older than its daily refresh interval. */
  stale: boolean;
  error?: string;
}

export interface PricingRequest {
  provider_id?: string;
  kind: string;
  base_url?: string;
  model: string;
}

export type PriceMap = Record<string, PriceEntry>;

export interface Pricing {
  prices: PriceMap;
  meta: PricingMeta | null;
  /** No table yet (still loading) or the Usage extension is unreachable. */
  unavailable: boolean;
}

/** Price to render for one request. `undefined` means "still resolving";
 * every other not-yet-priced state is an explicit verdict, so a surface
 * can never sit on a permanent spinner. */
export function selectPrice(
  pricing: Pricing,
  request: PricingRequest | null,
): PriceEntry | undefined {
  if (!request) return { available: false, reason: "model_unknown" };
  if (pricing.unavailable) return { available: false, reason: "pricing_unavailable" };
  return pricing.prices[priceKey(request)];
}

/** Extended cache writes are billed at 2x the base input rate (Anthropic
 * is the only provider that reports the 1h/5m split at all). The catalog
 * publishes a single `cache_write`, which is the 5m rate. */
const CACHE_WRITE_1H_INPUT_MULTIPLIER = 2;

/** Response key of the extension's POST /pricing. */
export function priceKey(request: {
  provider_id?: string;
  kind: string;
  model: string;
}): string {
  return `${request.provider_id || request.kind}::${request.model}`;
}

/** True when the provider reported the cache-write TTL split (Anthropic
 * only); absence means the split is unknown, not zero. */
export function hasCacheWriteBreakdown(usage: TokenUsage): boolean {
  return (
    usage.cache_creation_5m_tokens !== undefined ||
    usage.cache_creation_1h_tokens !== undefined
  );
}

export interface UsageCost {
  amount: number;
  /** A token bucket this usage actually used has no published rate, so
   * the figure is a floor rather than the full cost. */
  partial: boolean;
}

/** USD cost of `usage` at `entry`'s rates, or null when the model has no
 * published price. Null must render as "unknown", never as $0 — a made-up
 * number is worse than no number. */
export function usageCost(
  usage: TokenUsage,
  entry: PriceEntry | undefined,
): UsageCost | null {
  const cost = entry?.available ? entry.cost : undefined;
  if (!cost) return null;
  let partial = false;
  const perMillion = (tokens: number, rate: number | undefined): number => {
    if (rate === undefined) {
      // Charging an unpriced bucket at zero would understate the cost
      // silently; the caller says so instead.
      if (tokens > 0) partial = true;
      return 0;
    }
    return (tokens * rate) / 1_000_000;
  };

  const cacheWrite5m = cost.cache_write;
  const cacheWrite1h = cost.cache_write
    ? cost.input * CACHE_WRITE_1H_INPUT_MULTIPLIER
    : undefined;

  const cacheWrite = hasCacheWriteBreakdown(usage)
    ? perMillion(usage.cache_creation_5m_tokens ?? 0, cacheWrite5m) +
      perMillion(usage.cache_creation_1h_tokens ?? 0, cacheWrite1h)
    : perMillion(usage.cache_creation_input_tokens ?? 0, cacheWrite5m);

  const amount =
    perMillion(usage.input_tokens, cost.input) +
    perMillion(usage.output_tokens, cost.output) +
    perMillion(usage.cache_read_input_tokens ?? 0, cost.cache_read) +
    cacheWrite;
  return { amount, partial };
}

export function formatCost(cost: number, currency = "USD"): string {
  const symbol = currency === "USD" ? "$" : `${currency} `;
  if (cost > 0 && cost < 0.01) return `<${symbol}0.01`;
  return `${symbol}${cost.toFixed(2)}`;
}

/** USD per 1M tokens, for showing a model's rate next to its name. */
export function formatRate(rate: number, currency = "USD"): string {
  const symbol = currency === "USD" ? "$" : `${currency} `;
  const digits = rate > 0 && rate < 1 ? 2 : rate % 1 === 0 ? 0 : 2;
  return `${symbol}${rate.toFixed(digits)}`;
}
