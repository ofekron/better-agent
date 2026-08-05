import { fireEvent, render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ModelCatalogStatus } from "../src/components/ModelCatalogStatus";
import type { ModelCatalog } from "../src/types";

vi.mock("react-i18next", () => {
  const t = (k: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.model === "string" ? `${k}:${opts.model}` : k;
  return { useTranslation: () => ({ t }) };
});

const baseCatalog: ModelCatalog = {
  provider_id: "p",
  provider_generation: "g",
  authoritative: true,
  status: "current",
  models: ["a"],
  models_current: true,
  retired: [],
  retired_models: [],
  last_refreshed_at: 0,
  reason: "r",
  authority_fingerprint: "f",
  last_known_good: { provider_generation: "g", models: [], retired: [] },
};

function withStatus(status: ModelCatalog["status"], over: Partial<ModelCatalog> = {}): ModelCatalog {
  return { ...baseCatalog, status, ...over };
}

describe("ModelCatalogStatus", () => {
  it("renders offline LKG message when offline with a catalog", () => {
    const { container } = render(
      <ModelCatalogStatus catalog={baseCatalog} networkState="offline" />,
    );
    const box = container.querySelector(".model-catalog-status-offline");
    expect(box?.textContent).toBe("model.catalogOfflineLkg");
    expect(box?.getAttribute("role")).toBe("status");
  });

  it("renders offline message when offline without a catalog", () => {
    const { container } = render(
      <ModelCatalogStatus catalog={null} networkState="offline" />,
    );
    expect(container.querySelector(".model-catalog-status-offline")?.textContent)
      .toBe("model.catalogOffline");
  });

  it("renders nothing when not offline and no catalog", () => {
    const { container } = render(
      <ModelCatalogStatus catalog={null} networkState="online" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders a refresh button for current + authoritative catalog", () => {
    const onRefresh = vi.fn(async () => undefined);
    const { container } = render(
      <ModelCatalogStatus catalog={withStatus("current")} networkState="online" onRefresh={onRefresh} />,
    );
    const box = container.querySelector(".model-catalog-status-current");
    expect(box).not.toBeNull();
    const btn = box!.querySelector("button")!;
    expect(btn.textContent).toBe("files.refreshButton");
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("shows refreshing label and disables the button while refreshing", () => {
    const { container } = render(
      <ModelCatalogStatus
        catalog={withStatus("current")}
        networkState="online"
        onRefresh={vi.fn()}
        refreshing
      />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.textContent).toBe("progress.refreshing");
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("aria-busy")).toBe("true");
  });

  it("renders nothing for current when no refresh is available (non-authoritative)", () => {
    const { container } = render(
      <ModelCatalogStatus
        catalog={withStatus("current", { authoritative: false })}
        networkState="online"
        onRefresh={vi.fn()}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders error box for current with refreshError", () => {
    const { container } = render(
      <ModelCatalogStatus
        catalog={withStatus("current")}
        networkState="online"
        onRefresh={vi.fn()}
        refreshError="boom"
      />,
    );
    const box = container.querySelector(".model-catalog-status-error")!;
    expect(box.textContent).toContain("model.catalogError");
    expect(box.querySelector("button")).not.toBeNull();
  });

  it.each([
    ["pending", "model.catalogPending"],
    ["refreshing", "model.catalogRefreshing"],
    ["stale", "model.catalogStale"],
    ["unsupported", "model.catalogUnsupported"],
    ["unavailable", "model.catalogUnavailable"],
  ] as const)("renders status %s with its key and data-reason", (status, key) => {
    const { container } = render(
      <ModelCatalogStatus catalog={withStatus(status)} networkState="online" />,
    );
    const box = container.querySelector(`.model-catalog-status-${status}`)!;
    expect(box.getAttribute("data-reason")).toBe("r");
    expect(box.textContent).toContain(key);
  });

  it("shows error label for a non-current status when refreshError is set", () => {
    const { container } = render(
      <ModelCatalogStatus
        catalog={withStatus("stale")}
        networkState="online"
        onRefresh={vi.fn()}
        refreshError="boom"
      />,
    );
    expect(container.querySelector(".model-catalog-status-stale")?.textContent)
      .toContain("model.catalogError");
  });

  it("shows refresh button for a non-current authoritative status with onRefresh", () => {
    const { container } = render(
      <ModelCatalogStatus catalog={withStatus("stale")} networkState="online" onRefresh={vi.fn()} />,
    );
    expect(container.querySelector(".model-catalog-status-stale")?.querySelector("button"))
      .not.toBeNull();
  });

  it("hides the refresh button when catalog is unsupported even with onRefresh", () => {
    const { container } = render(
      <ModelCatalogStatus catalog={withStatus("unsupported")} networkState="online" onRefresh={vi.fn()} />,
    );
    expect(container.querySelector("button")).toBeNull();
  });

  it("swallows refresh promise rejection (void .catch)", async () => {
    const onRefresh = vi.fn(async () => {
      throw new Error("fail");
    });
    const { container } = render(
      <ModelCatalogStatus catalog={withStatus("current")} networkState="online" onRefresh={onRefresh} />,
    );
    const btn = container.querySelector("button")!;
    // Must not throw into the click handler.
    await expect(fireClickAndWait(btn)).resolves.toBeUndefined();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

async function fireClickAndWait(btn: HTMLElement) {
  fireEvent.click(btn);
  // Drain the rejected promise's .catch microtask.
  await Promise.resolve();
  await Promise.resolve();
}
