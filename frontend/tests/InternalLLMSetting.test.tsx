import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

// trackPromise is a passthrough so the real progress store (WS extender,
// retry) doesn't run; fetch is driven per-test.
vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

// Thin native-select stub: drives onChange directly and exposes the
// options/value/disabled the component passes, without re-testing Select.
vi.mock("../src/components/Select", () => ({
  Select: ({
    value,
    options,
    onChange,
    disabled,
  }: {
    value: string;
    options: { value: string; label: ReactNode }[];
    onChange: (v: string) => void;
    disabled?: boolean;
  }) => (
    <select
      className="bc-select-stub"
      data-disabled={disabled ? "true" : "false"}
      value={value}
      onChange={(e) => onChange((e.target as HTMLSelectElement).value)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {String(o.label)}
        </option>
      ))}
    </select>
  ),
}));

const { useRuntimeProfilesMock } = vi.hoisted(() => ({
  useRuntimeProfilesMock: vi.fn(),
}));
vi.mock("../src/hooks/useRuntimeProfiles", () => ({
  useRuntimeProfiles: () => useRuntimeProfilesMock(),
}));

import { InternalLLMSetting } from "../src/components/InternalLLMSetting";

// ---- fixtures ----
const P1 = {
  id: "prov1",
  custom_models: ["cm1"],
  runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "high"] as const }],
  reasoning_effort_options: ["low", "high"] as const,
} as never;
const RP1 = {
  id: "rp1",
  provider_id: "prov1",
  runner: "native",
  name: "Profile One",
  default_model: "dm1",
  deleted_at: null,
} as never;
const RP2 = {
  id: "rp2",
  provider_id: "prov1",
  runner: "native",
  name: "Dead",
  default_model: "dm2",
  deleted_at: "2024-01-01T00:00:00Z",
} as never;
const RP3 = {
  id: "rp3",
  provider_id: "prov1",
  runner: "native",
  name: "Profile Three",
  default_model: "dm3",
  deleted_at: null,
} as never;

const SNAPSHOT = {
  runtime_profiles: [RP1, RP2, RP3],
  default_runtime_profile_id: "rp1",
  last_models: { rp1: "lm1" },
} as never;

interface FetchCfg {
  load?: unknown;
  loadError?: boolean;
  providers?: unknown[];
  /** Return a providers body with NO `providers` key (exercises `|| []`). */
  providersMissing?: boolean;
  providersError?: boolean;
  putError?: boolean;
  putPromise?: Promise<unknown>;
}

function ok(json: unknown): { ok: boolean; status: number; json: () => Promise<unknown> } {
  return { ok: true, status: 200, json: async () => json };
}

function makeFetch(cfg: FetchCfg = {}): ReturnType<typeof vi.fn> {
  return vi.fn(async (url: unknown, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    if (method === "PUT" && u.includes("/internal-llm")) {
      if (cfg.putError) throw new Error("put failed");
      if (cfg.putPromise) return cfg.putPromise;
      return ok({});
    }
    if (u.includes("/internal-llm")) {
      if (cfg.loadError) throw new Error("load failed");
      return ok(cfg.load ?? {});
    }
    if (u.includes("/api/providers")) {
      if (cfg.providersError) throw new Error("providers failed");
      if (cfg.providersMissing) return ok({});
      return ok({ providers: cfg.providers ?? [P1] });
    }
    return ok({});
  });
}

function deferred<T = unknown>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function putCalls(fetchMock: ReturnType<typeof vi.fn>): { url: string; body: unknown }[] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([url, init]) => ({
      url: String(url),
      body: JSON.parse((init as RequestInit).body as string),
    }));
}

function optionValues(select: Element | undefined): string[] {
  if (!select) return [];
  return [...select.querySelectorAll("option")].map((o) => (o as HTMLOptionElement).value);
}

function rowSelects(container: HTMLElement, rowIdx = 0): Element[] {
  const rows = container.querySelectorAll(".internal-llm-row");
  return [...(rows[rowIdx]?.querySelectorAll("select.bc-select-stub") ?? [])];
}

function profilesState(overrides: Record<string, unknown> = {}): unknown {
  return {
    snapshot: SNAPSHOT,
    profiles: [RP1, RP3],
    defaultProfileId: "rp1",
    loading: false,
    error: null,
    refresh: vi.fn(),
    ...overrides,
  };
}

let originalFetch: typeof globalThis.fetch;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  useRuntimeProfilesMock.mockReturnValue(profilesState());
});

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

describe("InternalLLMSetting — mount & load", () => {
  it("renders nothing when there are no tasks (empty load body exercises the || defaults)", async () => {
    fetchMock = makeFetch({ load: {} });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.querySelector(".internal-llm-setting")).toBeNull();
  });

  it("renders hint + rows for loaded tasks and populates profile/model/effort selects", async () => {
    fetchMock = makeFetch({
      load: {
        tasks: ["search"],
        assignments: { search: { runtime_profile_id: "rp1", model: "m1", reasoning_effort: "low" } },
      },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    expect(container.querySelector(".context-strategy-hint")).toBeTruthy();
    // task label falls back to the task id
    expect(container.querySelector(".internal-llm-task")?.textContent).toBe("search");

    const selects = rowSelects(container);
    // profile / model / effort all present (provider has efforts)
    expect(selects).toHaveLength(3);
    expect(optionValues(selects[0])).toEqual(["", "rp1", "rp3"]); // inherit + live profiles
    expect(optionValues(selects[1])).toEqual(["", "m1", "dm1", "lm1", "cm1"]); // inherit + modelSet
    expect(optionValues(selects[2])).toEqual(["", "low", "high"]); // inherit + efforts
    expect((selects[0] as HTMLSelectElement).value).toBe("rp1");
    expect((selects[1] as HTMLSelectElement).value).toBe("m1");
    expect((selects[2] as HTMLSelectElement).value).toBe("low");
  });

  it("uses the task override instead of loaded tasks and hides the hint", async () => {
    fetchMock = makeFetch({ load: { tasks: ["loaded"], assignments: {} } });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting tasks={["override"]} showHint={false} />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    expect(container.querySelector(".context-strategy-hint")).toBeNull();
    expect(container.querySelector(".internal-llm-task")?.textContent).toBe("override");
  });

  it("keeps rendering from the override when the load fetch rejects (catch is a no-op)", async () => {
    fetchMock = makeFetch({ loadError: true });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting tasks={["search"]} />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    // no assignment loaded → profile resolves to default rp1, efforts shown
    expect(rowSelects(container)).toHaveLength(3);
  });

  it("hides the effort select and uses no provider when the providers fetch rejects", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { runtime_profile_id: "rp1" } } },
      providersError: true,
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const selects = rowSelects(container);
    expect(selects).toHaveLength(2); // profile + model only; effort hidden
    // no provider → only inherit option in model select (no custom_models/default sourced)
    expect(optionValues(selects[1])).toEqual(["", "dm1", "lm1"]);
  });

  it("hits the extension-scoped settings endpoint when extensionId is set", async () => {
    fetchMock = makeFetch({ load: { tasks: ["search"], assignments: {} } });
    globalThis.fetch = fetchMock as never;
    render(<InternalLLMSetting tasks={["search"]} extensionId="ext-1" />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const settingsCall = fetchMock.mock.calls
      .map(([u]) => String(u))
      .find((u) => u.includes("/internal-llm"));
    expect(settingsCall).toContain("/api/extensions/ext-1/internal-llm");
  });
});

describe("InternalLLMSetting — profile resolution & model set", () => {
  it("resolves an assignment profile that no longer exists to undefined (no efforts, no provider)", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { runtime_profile_id: "ghost" } } },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const selects = rowSelects(container);
    expect(selects).toHaveLength(2); // effort hidden: profile undefined → no efforts
    // no tombstone (ghost not found), no model sourced from a profile
    expect(optionValues(selects[0])).toEqual(["", "rp1", "rp3"]);
    expect(optionValues(selects[1])).toEqual([""]);
  });

  it("resolves to the default profile when an assignment has no profile id", async () => {
    useRuntimeProfilesMock.mockReturnValue(profilesState({ defaultProfileId: "rp3" }));
    fetchMock = makeFetch({ load: { tasks: ["search"], assignments: {} } });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    // default rp3 → dm3 default model + lm? none for rp3 → only dm3 (+inherit)
    const selects = rowSelects(container);
    expect(optionValues(selects[1])).toEqual(["", "dm3", "cm1"]);
  });

  it("resolves to undefined when there is no snapshot and no default", async () => {
    useRuntimeProfilesMock.mockReturnValue(profilesState({ snapshot: null, profiles: [], defaultProfileId: null }));
    fetchMock = makeFetch({ load: { tasks: ["search"], assignments: {} } });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const selects = rowSelects(container);
    expect(selects).toHaveLength(2); // no profile → no efforts
    expect(optionValues(selects[0])).toEqual([""]); // no live profiles
    expect(optionValues(selects[1])).toEqual([""]); // no model sources
  });

  it("badges an assigned-but-deleted profile as a tombstone option", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { runtime_profile_id: "rp2" } } },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const profileSelect = rowSelects(container)[0] as HTMLSelectElement;
    expect(profileSelect.value).toBe("rp2");
    expect(optionValues(profileSelect)).toEqual(["", "rp2", "rp1", "rp3"]); // inherit + tombstone + live
  });
});

describe("InternalLLMSetting — change (profile field)", () => {
  it("drops model + invalid effort when switching to a profile whose efforts lack the current effort", async () => {
    fetchMock = makeFetch({
      load: {
        tasks: ["search"],
        assignments: { search: { runtime_profile_id: "rp1", reasoning_effort: "medium", model: "m1" } },
      },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const profileSelect = rowSelects(container)[0] as HTMLSelectElement;
    fireEvent.change(profileSelect, { target: { value: "rp3" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    expect(putCalls(fetchMock)[0].body).toEqual({
      assignments: { search: { runtime_profile_id: "rp3" } },
    });
  });

  it("keeps a still-valid effort when switching profiles (effort in new opts)", async () => {
    fetchMock = makeFetch({
      load: {
        tasks: ["search"],
        assignments: { search: { runtime_profile_id: "rp1", reasoning_effort: "low", model: "m1" } },
      },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    fireEvent.change(rowSelects(container)[0] as HTMLSelectElement, { target: { value: "rp3" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    // effort kept, model dropped, profile changed
    expect(putCalls(fetchMock)[0].body).toEqual({
      assignments: { search: { runtime_profile_id: "rp3", reasoning_effort: "low" } },
    });
  });

  it("re-validates against the default profile when cleared to INHERIT", async () => {
    fetchMock = makeFetch({
      load: {
        tasks: ["search"],
        assignments: { search: { runtime_profile_id: "rp2", reasoning_effort: "high" } },
      },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    fireEvent.change(rowSelects(container)[0] as HTMLSelectElement, { target: { value: "" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    // profile cleared; default rp1 has low/high → high kept; no model to drop
    expect(putCalls(fetchMock)[0].body).toEqual({
      assignments: { search: { reasoning_effort: "high" } },
    });
  });

  it("clears to INHERIT with no default (empty id → undefined profile) and tolerates a providers body missing the key", async () => {
    useRuntimeProfilesMock.mockReturnValue(profilesState({ defaultProfileId: null }));
    fetchMock = makeFetch({
      load: {
        tasks: ["search"],
        assignments: { search: { runtime_profile_id: "rp1", reasoning_effort: "low", model: "m1" } },
      },
      providersMissing: true,
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    // no providers loaded → effort hidden even though a profile is assigned
    expect(rowSelects(container)).toHaveLength(2);
    fireEvent.change(rowSelects(container)[0] as HTMLSelectElement, { target: { value: "" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    // no default → id "" → no profile → no valid opts → effort dropped;
    // model dropped on profile change; profile cleared → entry empty → task removed
    expect(putCalls(fetchMock)[0].body).toEqual({ assignments: {} });
  });
});

describe("InternalLLMSetting — change (model / effort fields)", () => {
  it("sets a model on a previously-unassigned task and PUTs it", async () => {
    fetchMock = makeFetch({ load: { tasks: ["search"], assignments: {} } });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const modelSelect = rowSelects(container)[1] as HTMLSelectElement;
    fireEvent.change(modelSelect, { target: { value: "dm1" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    expect(putCalls(fetchMock)[0].body).toEqual({ assignments: { search: { model: "dm1" } } });
  });

  it("removes the task entry when its last field is cleared to INHERIT", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { model: "m1" } } },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    fireEvent.change(rowSelects(container)[1] as HTMLSelectElement, { target: { value: "" } });

    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    expect(putCalls(fetchMock)[0].body).toEqual({ assignments: {} });
  });

  it("sets and clears the reasoning effort without touching model/profile", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { model: "m1" } } },
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    const effortSelect = rowSelects(container)[2] as HTMLSelectElement;

    fireEvent.change(effortSelect, { target: { value: "low" } });
    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(1));
    expect(putCalls(fetchMock)[0].body).toEqual({
      assignments: { search: { model: "m1", reasoning_effort: "low" } },
    });

    fireEvent.change(effortSelect, { target: { value: "" } });
    await waitFor(() => expect(putCalls(fetchMock)).toHaveLength(2));
    // effort cleared → only model remains
    expect(putCalls(fetchMock)[1].body).toEqual({ assignments: { search: { model: "m1" } } });
  });
});

describe("InternalLLMSetting — save lifecycle", () => {
  it("disables selects while a PUT is in flight and re-enables on success", async () => {
    const pending = deferred<{ ok: boolean; status: number; json: () => Promise<unknown> }>();
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { model: "m1" } } },
      putPromise: pending.promise,
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    fireEvent.change(rowSelects(container)[1] as HTMLSelectElement, { target: { value: "dm1" } });

    await waitFor(() =>
      expect(rowSelects(container).map((s) => s.getAttribute("data-disabled"))).toEqual(["true", "true", "true"]),
    );

    pending.resolve({ ok: true, status: 200, json: async () => ({}) });
    await waitFor(() =>
      expect(rowSelects(container).map((s) => s.getAttribute("data-disabled"))).toEqual(["false", "false", "false"]),
    );
  });

  it("re-enables selects after a failed PUT (catch swallows, finally clears saving)", async () => {
    fetchMock = makeFetch({
      load: { tasks: ["search"], assignments: { search: { model: "m1" } } },
      putError: true,
    });
    globalThis.fetch = fetchMock as never;
    const { container } = render(<InternalLLMSetting />);

    await waitFor(() => expect(container.querySelector(".internal-llm-row")).toBeTruthy());
    fireEvent.change(rowSelects(container)[1] as HTMLSelectElement, { target: { value: "dm1" } });

    await waitFor(() =>
      expect(rowSelects(container).map((s) => s.getAttribute("data-disabled"))).toEqual(["false", "false", "false"]),
    );
    // PUT was attempted (and its body captured) even though it rejected
    expect(putCalls(fetchMock)).toHaveLength(1);
  });
});
