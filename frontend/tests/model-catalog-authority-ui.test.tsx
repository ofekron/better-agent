import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { ModelCatalogStatus } from "../src/components/ModelCatalogStatus";
import { ModelCatalogActivity } from "../src/components/ModelCatalogActivity";
import { useProviderModelCatalog } from "../src/hooks/useProviderModelCatalog";
import { eventBus } from "../src/lib/eventBus";
import type { ModelCatalog } from "../src/types";
import { readProviderCache } from "../src/utils/providerCache";
import "../src/i18n";


const CATALOG: ModelCatalog = {
  provider_id: "codex",
  provider_generation: "generation-1",
  authoritative: true,
  status: "stale",
  models: ["gpt-current"],
  models_current: false,
  retired: ["gpt-retired"],
  retired_models: [{ id: "gpt-retired", first_absent_at: 42 }],
  last_refreshed_at: 100,
  reason: "source_changed",
  authority_fingerprint: "sha256:authority",
  last_known_good: {
    models: ["gpt-current"],
    last_refreshed_at: 100,
  },
  runtime_profiles: [],
};


const ACTIVITY_PROVIDERS = [
  { id: "codex", name: "Codex", nickname: "work" },
] as unknown as Parameters<typeof ModelCatalogActivity>[0]["providers"];


function response(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 503,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}


function HookHarness() {
  const { catalog, networkState, refresh } = useProviderModelCatalog("codex");
  return (
    <>
      <span data-testid="status">{catalog?.status ?? networkState}</span>
      <span data-testid="models">{catalog?.models.join(",") ?? ""}</span>
      <button onClick={refresh}>refresh</button>
      <ModelCatalogStatus catalog={catalog} networkState={networkState} />
    </>
  );
}


describe("authoritative model catalog UI", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => response(CATALOG)));
  });

  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("rejects the former frontend-owned model catalog cache", () => {
    localStorage.setItem("better-agent-provider-cache", JSON.stringify({
      version: 4,
      defaultProviderId: null,
      providers: [],
      modelsByProvider: { codex: ["frontend-shadow"] },
    }));
    expect(readProviderCache()).toBeNull();
  });

  it("renders stale LKG distinctly and refetches on a projection WS fact", async () => {
    render(<HookHarness />);
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("stale"));
    expect(screen.getByText(/last-known/i)).toBeTruthy();

    const next = { ...CATALOG, status: "current" as const, models_current: true, reason: "" };
    vi.mocked(fetch).mockResolvedValueOnce(response(next));
    act(() => {
      eventBus.publish("models_catalog_changed", {
        provider_id: "codex",
        projection: next,
      });
    });
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("current"));
  });

  it("keeps the backend snapshot but reports offline after a refetch failure", async () => {
    render(<HookHarness />);
    await waitFor(() => expect(screen.getByTestId("models").textContent).toBe("gpt-current"));

    vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
    act(() => {
      eventBus.publish("models_catalog_changed", { provider_id: "codex" });
    });
    await waitFor(() => expect(screen.getByText(/offline/i)).toBeTruthy());
    expect(screen.getByTestId("models").textContent).toBe("gpt-current");
  });

  it("manual refresh stops blocking after the backend acknowledges it", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(response(CATALOG))
      .mockResolvedValueOnce(
        response({
          accepted: true,
          provider_id: "codex",
          provider_generation: "generation-1",
          catalog: CATALOG,
        }),
      );
    render(<HookHarness />);
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("stale"));
    fireEvent.click(screen.getByText("refresh"));
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/providers/codex/models/refresh"),
        { method: "POST" },
      );
    });
  });

  it("shows refreshing background work in a dismissible RTL-safe popup", async () => {
    const refreshing = { ...CATALOG, status: "refreshing" as const };
    vi.mocked(fetch).mockResolvedValueOnce(response({ catalogs: [refreshing] }));
    const { container } = render(
      <ModelCatalogActivity providers={ACTIVITY_PROVIDERS} />,
    );
    await waitFor(() => {
      expect(container.querySelector(".model-catalog-activity")).not.toBeNull();
    });
    expect(container.querySelector('[role="status"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(container.querySelector(".model-catalog-activity")).toBeNull();
  });

  it("refreshes background activity from a catalog bus fact", async () => {
    const refreshing = { ...CATALOG, status: "refreshing" as const };
    vi.mocked(fetch)
      .mockResolvedValueOnce(response({ catalogs: [] }))
      .mockResolvedValueOnce(response({ catalogs: [refreshing] }));
    const { container } = render(
      <ModelCatalogActivity providers={ACTIVITY_PROVIDERS} />,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    act(() => {
      eventBus.publish("models_catalog_changed", { provider_id: "codex" });
    });

    await waitFor(() => {
      expect(container.querySelector(".model-catalog-activity")).not.toBeNull();
    });
  });

  it("names the provider and the specific reason for each activity row", async () => {
    const refreshing = {
      ...CATALOG,
      status: "refreshing" as const,
      reason: "provider_changed",
    };
    vi.mocked(fetch).mockResolvedValueOnce(response({ catalogs: [refreshing] }));
    const { container } = render(
      <ModelCatalogActivity providers={ACTIVITY_PROVIDERS} />,
    );
    await waitFor(() => {
      expect(
        container.querySelector(".model-catalog-activity-provider"),
      ).not.toBeNull();
    });
    expect(
      container.querySelector(".model-catalog-activity-provider")?.textContent,
    ).toBe("Codex · work");
    expect(
      container.querySelector(".model-catalog-activity-detail")?.textContent,
    ).toBe("Provider settings changed — rediscovering models");
  });

  it("falls back to the provider id and status when the provider is unknown", async () => {
    const refreshing = {
      ...CATALOG,
      provider_id: "13831e83",
      status: "refreshing" as const,
      reason: "",
    };
    vi.mocked(fetch).mockResolvedValueOnce(response({ catalogs: [refreshing] }));
    const { container } = render(<ModelCatalogActivity providers={[]} />);
    await waitFor(() => {
      expect(
        container.querySelector(".model-catalog-activity-provider"),
      ).not.toBeNull();
    });
    expect(
      container.querySelector(".model-catalog-activity-provider")?.textContent,
    ).toBe("13831e83");
    expect(
      container.querySelector(".model-catalog-activity-detail")?.textContent,
    ).toBe("Refreshing models…");
  });

  it("uses logical RTL positioning and disables motion when requested", () => {
    const css = readFileSync("src/styles/globals.css", "utf8");
    expect(css).toMatch(
      /\.model-catalog-activity\s*{[^}]*inset-inline-end:\s*24px;/s,
    );
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*{[^}]*\.model-catalog-activity,/s,
    );
  });

  it("ships every catalog-state key in every locale", () => {
    const required = [
      "model.catalogPending",
      "model.catalogRefreshing",
      "model.catalogStale",
      "model.catalogError",
      "model.catalogUnsupported",
      "model.catalogUnavailable",
      "model.catalogOffline",
      "model.catalogOfflineLkg",
      "model.catalogActivityTitle",
      "model.catalogActivityDismiss",
      "model.catalogRefreshAccepted",
      "model.catalogActivity.pending",
      "model.catalogActivity.refreshing",
      "model.catalogActivity.stale",
      "model.catalogActivity.error",
      "model.catalogActivity.unsupported",
      "model.catalogActivity.unavailable",
      "model.catalogReason.catalog_pending",
      "model.catalogReason.provider_changed",
      "model.catalogReason.source_changed",
      "model.catalogReason.watcher_unavailable",
      "model.catalogReason.refresh_failed",
      "model.catalogReason.no_provider",
      "model.notSelectable",
      "setup.defaultModelCustomPlaceholder",
    ];
    for (const file of readdirSync("src/i18n").filter((name) => name.endsWith(".json"))) {
      const locale = JSON.parse(readFileSync(join("src/i18n", file), "utf8"));
      for (const key of required) {
        expect(locale[key], `${file}:${key}`).toBeTypeOf("string");
        expect(locale[key].trim(), `${file}:${key}`).not.toBe("");
      }
    }
  });

  it.each([
    ["pending", /loading/i],
    ["refreshing", /refreshing/i],
    ["error", /couldn.t refresh/i],
    ["unsupported", /not supported/i],
    ["unavailable", /unavailable/i],
  ] as const)("renders the %s backend state", (status, expected) => {
    render(
      <ModelCatalogStatus
        catalog={{ ...CATALOG, status, reason: `${status}_reason` }}
        networkState="online"
      />,
    );
    expect(screen.getByText(expected)).toBeTruthy();
  });
});
