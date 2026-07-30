import { describe, expect, it } from "vitest";
import {
  formatCost,
  formatRate,
  priceKey,
  selectPrice,
  usageCost,
  type PriceEntry,
  type Pricing,
} from "src/utils/pricing";
import type { TokenUsage } from "src/types";

const usage = (over: Partial<TokenUsage> = {}): TokenUsage => ({
  input_tokens: 1_000_000,
  output_tokens: 1_000_000,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: 0,
  ...over,
});

const anthropic: PriceEntry = {
  available: true,
  basis: "provider_catalog",
  catalog_provider: "anthropic",
  catalog_model: "claude-sonnet-4-6",
  currency: "USD",
  cost: { input: 3, output: 15, cache_read: 0.3, cache_write: 3.75 },
};

describe("usageCost", () => {
  it("charges each bucket at its published rate", () => {
    const cost = usageCost(usage(), anthropic);
    expect(cost).toEqual({ amount: 18, partial: false });
  });

  it("uses the 1h multiplier only for the reported extended-cache split", () => {
    const cost = usageCost(
      usage({ cache_creation_5m_tokens: 1_000_000, cache_creation_1h_tokens: 1_000_000 }),
      anthropic,
    );
    // 3 + 15 + 3.75 (5m) + 6 (1h = 2x input)
    expect(cost?.amount).toBeCloseTo(27.75, 5);
  });

  it("falls back to the undifferentiated cache-write bucket when there is no split", () => {
    const cost = usageCost(usage({ cache_creation_input_tokens: 1_000_000 }), anthropic);
    expect(cost?.amount).toBeCloseTo(21.75, 5);
  });

  it("flags a figure that skipped a bucket with no published rate", () => {
    const noCacheWrite: PriceEntry = {
      ...anthropic,
      cost: { input: 3, output: 15, cache_read: 0.3 },
    };
    const cost = usageCost(usage({ cache_creation_input_tokens: 1_000_000 }), noCacheWrite);
    // The unpriced bucket contributes nothing, and the caller is told.
    expect(cost).toEqual({ amount: 18, partial: true });
  });

  it("returns null rather than a fabricated number when the model is unpriced", () => {
    expect(usageCost(usage(), { available: false, reason: "model_not_priced" })).toBeNull();
    expect(usageCost(usage(), undefined)).toBeNull();
  });
});

describe("selectPrice", () => {
  const pricing = (over: Partial<Pricing> = {}): Pricing => ({
    prices: {},
    meta: null,
    unavailable: false,
    ...over,
  });
  const request = { provider_id: "claude-main", kind: "claude", model: "sonnet" };

  it("keys into the batch response the same way the backend does", () => {
    expect(priceKey(request)).toBe("claude-main::sonnet");
    expect(priceKey({ kind: "claude", model: "sonnet" })).toBe("claude::sonnet");
    const found = selectPrice(pricing({ prices: { "claude-main::sonnet": anthropic } }), request);
    expect(found).toBe(anthropic);
  });

  it("stays undefined only while the price is genuinely still resolving", () => {
    expect(selectPrice(pricing(), request)).toBeUndefined();
  });

  it("resolves to an explicit verdict when the model or the service is unknown", () => {
    expect(selectPrice(pricing(), null)?.reason).toBe("model_unknown");
    expect(selectPrice(pricing({ unavailable: true }), request)?.reason).toBe(
      "pricing_unavailable",
    );
  });
});

describe("formatting", () => {
  it("never rounds a real cost down to zero", () => {
    expect(formatCost(0.004)).toBe("<$0.01");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(12.3456)).toBe("$12.35");
  });

  it("renders rates readably in both magnitudes", () => {
    expect(formatRate(0.3)).toBe("$0.30");
    expect(formatRate(15)).toBe("$15");
    expect(formatRate(3.75)).toBe("$3.75");
  });
});
