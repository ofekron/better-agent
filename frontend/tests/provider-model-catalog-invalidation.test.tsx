import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useProviderCatalogRevision } from "../src/hooks/useModelsCatalogChanged";
import { eventBus } from "../src/lib/eventBus";

describe("provider model catalog invalidation", () => {
  it("increments only for the bound provider", () => {
    const { result } = renderHook(() => useProviderCatalogRevision("provider-a"));

    act(() => {
      eventBus.publish("models_catalog_changed", { provider_id: "provider-b" });
    });
    expect(result.current).toBe(0);

    act(() => {
      eventBus.publish("models_catalog_changed", { provider_id: "provider-a" });
    });
    expect(result.current).toBe(1);
  });
});
