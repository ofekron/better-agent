import { fireEvent, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, it, expect, vi } from "vitest";
import {
  EffortDefaultField,
  ModelDefaultField,
} from "../src/components/RuntimeProfileFields";
import type { ModelCatalog } from "../src/types";
import type {
  CatalogNetworkState,
} from "../src/hooks/useProviderModelCatalog";

vi.mock("react-i18next", () => {
  const t = (k: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.model === "string" ? `${k}:${opts.model}` : k;
  return { useTranslation: () => ({ t }) };
});

// Native <select> stub (#56) — exposes options/value/onChange without re-testing Select.
vi.mock("../src/components/Select", () => ({
  Select: ({
    value,
    options,
    onChange,
  }: {
    value: string;
    options: { value: string; label: ReactNode; disabled?: boolean }[];
    onChange: (v: string) => void;
  }) => (
    <select
      className="bc-select-stub"
      value={value}
      onChange={(e) => onChange((e.target as HTMLSelectElement).value)}
    >
      {options.map((o) => (
        <option
          key={o.value}
          value={o.value}
          disabled={o.disabled}
          data-disabled={o.disabled ? "true" : "false"}
        >
          {String(o.label)}
        </option>
      ))}
    </select>
  ),
}));

// Marker stub — verifies catalogState is destructured and forwarded correctly.
vi.mock("../src/components/ModelCatalogStatus", () => ({
  ModelCatalogStatus: (props: Record<string, unknown>) => (
    <div
      data-testid="catalog-status"
      data-network={String(props.networkState)}
      data-refreshing={String(props.refreshing)}
      data-has-refresh-error={props.refreshError ? "true" : "false"}
      data-has-catalog={props.catalog ? "true" : "false"}
    >
      {props.onRefresh ? "has-refresh" : "no-refresh"}
    </div>
  ),
}));

type CatalogState = {
  catalog: ModelCatalog | null;
  networkState: CatalogNetworkState;
  refresh: () => void;
  refreshing: boolean;
  refreshError: string | null;
};

const baseCatalog: ModelCatalog = {
  provider_id: "p",
  provider_generation: "g",
  authoritative: true,
  status: "current",
  models: ["alpha", "beta"],
  models_current: true,
  retired: [],
  retired_models: [],
  last_refreshed_at: 0,
  reason: "r",
  authority_fingerprint: "f",
  last_known_good: { provider_generation: "g", models: [], retired: [] },
};

function catalogState(over: Partial<CatalogState> = {}): CatalogState {
  return {
    catalog: baseCatalog,
    networkState: "online",
    refresh: vi.fn(),
    refreshing: false,
    refreshError: null,
    ...over,
  };
}

function selectOptions(container: HTMLElement): HTMLOptionElement[] {
  return [
    ...(container.querySelector("select.bc-select-stub")?.querySelectorAll(
      "option",
    ) ?? []),
  ] as HTMLOptionElement[];
}

describe("ModelDefaultField", () => {
  it("renders a Select of catalog models when model is in the list", () => {
    const onModelChange = vi.fn();
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState()}
        model="alpha"
        onModelChange={onModelChange}
      />,
    );
    const select = container.querySelector(
      "select.bc-select-stub",
    ) as HTMLSelectElement;
    expect(select.value).toBe("alpha");
    const opts = selectOptions(container);
    // No injected disabled placeholder when model is in the list.
    expect(opts).toHaveLength(2);
    expect(opts.map((o) => o.value)).toEqual(["alpha", "beta"]);
    expect(opts.every((o) => o.getAttribute("data-disabled") === "false")).toBe(
      true,
    );
  });

  it("injects a disabled not-in-list placeholder labelled with the model", () => {
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState()}
        model="custom-x"
        onModelChange={vi.fn()}
      />,
    );
    const select = container.querySelector(
      "select.bc-select-stub",
    ) as HTMLSelectElement;
    expect(select.value).toBe("");
    const opts = selectOptions(container);
    expect(opts).toHaveLength(3);
    expect(opts[0].value).toBe("");
    expect(opts[0].getAttribute("data-disabled")).toBe("true");
    expect(opts[0].textContent).toBe("setup.defaultModelNotInList:custom-x");
  });

  it("injects the select placeholder when model is empty and not in the list", () => {
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState()}
        model=""
        onModelChange={vi.fn()}
      />,
    );
    const opts = selectOptions(container);
    expect(opts[0].value).toBe("");
    expect(opts[0].textContent).toBe("setup.defaultModelSelectPlaceholder");
  });

  it("fires onModelChange when a different model is selected", () => {
    const onModelChange = vi.fn();
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState()}
        model="alpha"
        onModelChange={onModelChange}
      />,
    );
    const select = container.querySelector(
      "select.bc-select-stub",
    ) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "beta" } });
    expect(onModelChange).toHaveBeenCalledWith("beta");
  });

  it("falls back to a text input when the catalog has no models", () => {
    const onModelChange = vi.fn();
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState({
          catalog: { ...baseCatalog, models: [] },
        })}
        model="free-text"
        onModelChange={onModelChange}
      />,
    );
    const input = container.querySelector(
      "input[type='text']",
    ) as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.value).toBe("free-text");
    expect(input.getAttribute("spellcheck")).toBe("false");
    expect(input.placeholder).toBe("setup.defaultModelCustomPlaceholder");
    expect(container.querySelector("select.bc-select-stub")).toBeNull();
    fireEvent.change(input, { target: { value: "typed" } });
    expect(onModelChange).toHaveBeenCalledWith("typed");
  });

  it("falls back to a text input when catalog is null", () => {
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState({ catalog: null })}
        model="m"
        onModelChange={vi.fn()}
      />,
    );
    expect(
      container.querySelector("input[type='text']"),
    ).toBeTruthy();
  });

  it("forwards catalogState fields to ModelCatalogStatus", () => {
    const refresh = vi.fn();
    const { getByTestId } = render(
      <ModelDefaultField
        catalogState={catalogState({
          networkState: "offline",
          refreshing: true,
          refreshError: "boom",
          refresh,
        })}
        model="alpha"
        onModelChange={vi.fn()}
      />,
    );
    const status = getByTestId("catalog-status");
    expect(status.getAttribute("data-network")).toBe("offline");
    expect(status.getAttribute("data-refreshing")).toBe("true");
    expect(status.getAttribute("data-has-refresh-error")).toBe("true");
    expect(status.getAttribute("data-has-catalog")).toBe("true");
    expect(status.textContent).toBe("has-refresh");
  });

  it("renders the i18n label for the model field", () => {
    const { container } = render(
      <ModelDefaultField
        catalogState={catalogState()}
        model="alpha"
        onModelChange={vi.fn()}
      />,
    );
    expect(container.querySelector("label")?.textContent).toBe(
      "runtimeProfile.defaultModelLabel",
    );
  });
});

describe("EffortDefaultField", () => {
  it("renders nothing when there are no effort options", () => {
    const { container } = render(
      <EffortDefaultField
        options={[]}
        effort=""
        onEffortChange={vi.fn()}
      />,
    );
    expect(container.querySelector(".setup-field")).toBeNull();
  });

  it("renders a Select of effort options and casts the change value", () => {
    const onEffortChange = vi.fn();
    const { container } = render(
      <EffortDefaultField
        options={["low", "high"]}
        effort="low"
        onEffortChange={onEffortChange}
      />,
    );
    const select = container.querySelector(
      "select.bc-select-stub",
    ) as HTMLSelectElement;
    expect(select.value).toBe("low");
    const opts = selectOptions(container);
    expect(opts.map((o) => o.textContent)).toEqual([
      "reasoningEffort.low",
      "reasoningEffort.high",
    ]);
    fireEvent.change(select, { target: { value: "high" } });
    expect(onEffortChange).toHaveBeenCalledWith("high");
  });

  it("renders the i18n label for the effort field", () => {
    const { container } = render(
      <EffortDefaultField
        options={["low"]}
        effort="low"
        onEffortChange={vi.fn()}
      />,
    );
    expect(container.querySelector("label")?.textContent).toBe(
      "runtimeProfile.defaultEffortLabel",
    );
  });
});
