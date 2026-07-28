import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useProviderCatalogRevision } from "../src/hooks/useModelsCatalogChanged";

describe("provider model catalog invalidation", () => {
  it("increments only for the bound provider", () => {
    const { result } = renderHook(() => useProviderCatalogRevision("provider-a"));

    act(() => {
      window.dispatchEvent(new CustomEvent("models_catalog_changed", {
        detail: { provider_id: "provider-b" },
      }));
    });
    expect(result.current).toBe(0);

    act(() => {
      window.dispatchEvent(new CustomEvent("models_catalog_changed", {
        detail: { provider_id: "provider-a" },
      }));
    });
    expect(result.current).toBe(1);
  });
});
