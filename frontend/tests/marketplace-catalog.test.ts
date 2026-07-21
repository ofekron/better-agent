import { describe, expect, it } from "vitest";
import "../../extensions/marketplace/ui/catalog.js";

const { isMarketplaceManaged, marketplaceRows } = (globalThis as typeof globalThis & {
  marketplaceCatalog: {
    isMarketplaceManaged: (record: unknown) => boolean;
    marketplaceRows: (catalog: Array<{ id: string; name?: string }>, installed: unknown[], query?: string) => Array<{ id: string }>;
  };
}).marketplaceCatalog;

describe("marketplace catalog projection", () => {
  it("keeps catalog ordering, deduplicates installed overlap, and appends installed-only extensions", () => {
    const catalog = [
      { id: "ofek.alpha", name: "Alpha" },
      { id: "ofek.beta", name: "Beta" },
    ];
    const installed = [
      { manifest: { id: "ofek.beta", name: "Installed Beta" }, source: { type: "marketplace" } },
      { manifest: { id: "ofek.testape", name: "TestApe" }, source: { type: "private_local" } },
    ];

    expect(marketplaceRows(catalog, installed).map((item) => item.id)).toEqual([
      "ofek.alpha",
      "ofek.beta",
      "ofek.testape",
    ]);
  });

  it("filters installed-only rows with the active query without dropping server-filtered catalog rows", () => {
    const catalog = [{ id: "ofek.adv", name: "ADV" }];
    const installed = [
      { manifest: { id: "ofek.testape", name: "TestApe", description: "UI testing" } },
      { manifest: { id: "ofek.scheduler", name: "Scheduler" } },
    ];

    expect(marketplaceRows(catalog, installed, "test").map((item) => item.id)).toEqual([
      "ofek.adv",
      "ofek.testape",
    ]);
  });

  it("allows management only for canonical marketplace installs", () => {
    expect(isMarketplaceManaged({ source: { type: "marketplace" } })).toBe(true);
    expect(isMarketplaceManaged({ source: { type: "artifact" } })).toBe(false);
    expect(isMarketplaceManaged({ source: { type: "private_local" } })).toBe(false);
  });
});
