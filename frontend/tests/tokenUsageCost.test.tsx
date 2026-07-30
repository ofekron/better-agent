import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TokenUsageDisplay } from "../src/components/TokenUsage";
import type { PriceEntry } from "../src/utils/pricing";
import type { TokenUsage } from "../src/types";

// The real interpolation matters here: the tooltip must name the resolved
// model and its rates, so a key-only stub would not prove anything.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (_key: string, options?: Record<string, unknown>) => {
      const raw = String(options?.defaultValue ?? "");
      return raw.replace(/\{\{(\w+)\}\}/g, (_m, name) => String(options?.[name] ?? ""));
    },
  }),
}));

const usage: TokenUsage = {
  input_tokens: 2_000_000,
  output_tokens: 1_000_000,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: 0,
};

const priced: PriceEntry = {
  available: true,
  basis: "provider_catalog",
  catalog_provider: "anthropic",
  catalog_model: "claude-sonnet-5",
  currency: "USD",
  cost: { input: 2, output: 10, cache_read: 0.2, cache_write: 2.5 },
};

describe("TokenUsageDisplay cost row", () => {
  it("renders the cost computed from the model's published rates", () => {
    render(<TokenUsageDisplay usage={usage} price={priced} />);
    // 2M input @ $2 + 1M output @ $10 = $14.00
    expect(screen.getByText("$14.00")).toBeTruthy();
  });

  it("names the resolved catalog model and its rates in the tooltip", () => {
    render(<TokenUsageDisplay usage={usage} price={priced} />);
    expect(screen.getByText("$14.00").getAttribute("title")).toBe(
      "claude-sonnet-5 · $2 in / $10 out per 1M tokens",
    );
  });

  it("says the price is still resolving instead of showing a number", () => {
    render(<TokenUsageDisplay usage={usage} />);
    expect(screen.getByText("Pricing…")).toBeTruthy();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("says the model has no published price instead of inventing one", () => {
    render(
      <TokenUsageDisplay usage={usage} price={{ available: false, reason: "model_not_priced" }} />,
    );
    expect(screen.getByText("No published price")).toBeTruthy();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("marks a figure taken from the underlying model's list price", () => {
    render(
      <TokenUsageDisplay usage={usage} price={{ ...priced, basis: "underlying_vendor_list" }} />,
    );
    expect(screen.getByText("model list price")).toBeTruthy();
  });

  it("marks a figure that skipped a bucket with no published rate", () => {
    render(
      <TokenUsageDisplay
        usage={{ ...usage, cache_creation_input_tokens: 1_000_000 }}
        price={{ ...priced, cost: { input: 2, output: 10, cache_read: 0.2 } }}
      />,
    );
    expect(screen.getByText("partial — some rates unpublished")).toBeTruthy();
  });
});
