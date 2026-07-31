import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";

// trackedFetch is the only import from progress/store the component uses; drive
// it per-test to exercise the providers-load ok / not-ok / thrown arms.
const { trackedFetchMock } = vi.hoisted(() => ({ trackedFetchMock: vi.fn() }));
vi.mock("../src/progress/store", () => ({ trackedFetch: (...a: unknown[]) => trackedFetchMock(...a) }));

// Hooks are mocked so catalog / runtime-profile state is deterministic and no
// network runs; the component only reads their return values.
const { catalogMock, runtimeProfilesMock } = vi.hoisted(() => ({
  catalogMock: vi.fn(),
  runtimeProfilesMock: vi.fn(),
}));
vi.mock("../src/hooks/useProviderModelCatalog", () => ({
  useProviderModelCatalog: (providerId: string) => catalogMock(providerId),
}));
vi.mock("../src/hooks/useRuntimeProfiles", () => ({
  useRuntimeProfiles: () => runtimeProfilesMock(),
}));

// Child status widget is out of scope; stub it so the ModelCatalog shape is
// not exercised here.
vi.mock("../src/components/ModelCatalogStatus", () => ({
  ModelCatalogStatus: () => <div data-testid="catalog-status" />,
}));

import { HarnessProfileMeta } from "../src/components/harness/HarnessProfileMeta";
import type { HarnessProfile } from "../src/types";
import type { HarnessFieldWrite } from "../src/components/harness/types";

// ---- fixtures ----
const P1 = {
  id: "prov1",
  runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "high"] as const }],
  reasoning_effort_options: ["low", "high"] as const,
} as never;

const RP_LIVE = {
  id: "rp1",
  provider_id: "prov1",
  runner: "native",
  name: "Profile One",
  default_model: "dm1",
  deleted_at: null,
} as never;

const RP_TOMB = {
  id: "rp2",
  provider_id: "prov1",
  runner: "native",
  name: "Dead",
  default_model: "dm2",
  deleted_at: "2024-01-01T00:00:00Z",
} as never;

const SNAPSHOT = {
  runtime_profiles: [RP_LIVE, RP_TOMB],
  default_runtime_profile_id: "rp1",
} as never;

function profile(overrides: Partial<HarnessProfile> = {}): HarnessProfile {
  return {
    id: "self",
    name: "Self",
    description: "",
    revision: "r1",
    created_at: "",
    updated_at: "",
    fields: {
      extension_instances: {},
      disabled_builtin_tools: { value: [] } as never,
      disabled_builtin_extensions: { value: [] } as never,
      disabled_runtime_skills: { value: [] } as never,
      instruction_sources: {},
    },
    mcp_overrides: {},
    skill_overrides: {},
    native_harness_overrides: {},
    provider_run_config_overlay: {},
    ...overrides,
  } as never;
}

interface Cfg {
  providersOk?: boolean;
  providersBody?: unknown;
  providersThrow?: boolean;
  catalog?: { models: string[] } | null;
  snapshot?: unknown;
  liveProfiles?: unknown[];
}

function ok(json: unknown) {
  return { ok: true, status: 200, json: async () => json };
}

function applyCfg(cfg: Cfg) {
  trackedFetchMock.mockImplementation(async () => {
    if (cfg.providersThrow) throw new Error("net");
    if (cfg.providersOk === false) return { ok: false, status: 500, json: async () => ({}) };
    return ok(cfg.providersBody ?? { providers: [P1] });
  });
  catalogMock.mockReturnValue({
    catalog: cfg.catalog === undefined ? { models: ["m-cat"] } : cfg.catalog,
    networkState: "online",
    refresh: vi.fn(),
    refreshing: false,
    refreshError: null,
  });
  runtimeProfilesMock.mockReturnValue({
    snapshot: cfg.snapshot === undefined ? SNAPSHOT : cfg.snapshot,
    profiles: cfg.liveProfiles ?? [RP_LIVE],
  });
}

function optionValues(select: Element | null | undefined): string[] {
  if (!select) return [];
  return [...select.querySelectorAll("option")].map((o) => (o as HTMLOptionElement).value);
}

/** Selects in render order: base, runtime-profile, model, effort. */
function selects(container: HTMLElement): HTMLSelectElement[] {
  return [...container.querySelectorAll("article.harness-profile-meta select.harness-setting-input")] as HTMLSelectElement[];
}

function renderMeta(props: {
  profile?: HarnessProfile;
  profiles?: { id: string; name: string }[];
  disabled?: boolean;
  onWrite?: (w: HarnessFieldWrite) => void;
} = {}) {
  const onWrite = props.onWrite ?? vi.fn();
  const utils = render(
    <HarnessProfileMeta
      profile={props.profile ?? profile()}
      profiles={props.profiles ?? [
        { id: "self", name: "Self" },
        { id: "default", name: "Default" },
        { id: "other", name: "Other" },
      ]}
      disabled={props.disabled ?? false}
      onWrite={onWrite}
    />,
  );
  return { ...utils, onWrite };
}

beforeEach(() => applyCfg({}));
afterEach(() => vi.restoreAllMocks());

describe("HarnessProfileMeta — providers load", () => {
  it("fetches /api/providers on mount and populates efforts once a profile is pinned", async () => {
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    expect(trackedFetchMock.mock.calls[0][1]).toContain("/api/providers");
    // pinned rp1 → native runner → efforts low/high (real effortsForRunner)
    expect(optionValues(selects(container)[3])).toEqual(["", "low", "high"]);
  });

  it("falls back to [] when the providers body has no `providers` key", async () => {
    applyCfg({ providersBody: {} });
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    // no provider resolved → no efforts → effort select is only the inherit option
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });

  it("hits the not-ok branch (res.ok false) and settles on [] providers", async () => {
    applyCfg({ providersOk: false });
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    // no providers → effort select empty
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });

  it("hits the catch branch when trackedFetch throws and settles on [] providers", async () => {
    applyCfg({ providersThrow: true });
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });

  it("ignores a providers response resolved after unmount (cancelled guard in then)", async () => {
    let resolveRes!: (v: unknown) => void;
    trackedFetchMock.mockReturnValueOnce(new Promise((r) => { resolveRes = r; }));
    const { container, unmount } = renderMeta();
    const article = container.querySelector("article.harness-profile-meta");
    unmount(); // cleanup flips cancelled before the promise settles
    resolveRes(ok({ providers: [P1] }));
    // drain microtasks; the cancelled guard must skip the post-unmount setState
    await Promise.resolve();
    await Promise.resolve();
    expect(document.body.contains(article)).toBe(false);
  });

  it("ignores a providers rejection settled after unmount (cancelled guard in catch)", async () => {
    let rejectRes!: (e: unknown) => void;
    trackedFetchMock.mockReturnValueOnce(new Promise((_, rej) => { rejectRes = rej; }));
    const { container, unmount } = renderMeta();
    const article = container.querySelector("article.harness-profile-meta");
    unmount();
    rejectRes(new Error("late net"));
    await Promise.resolve();
    await Promise.resolve();
    expect(document.body.contains(article)).toBe(false);
  });
});

describe("HarnessProfileMeta — base profile", () => {
  it("excludes self and the synthesized Default from the base options", async () => {
    const { container } = renderMeta();
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    // only "" (none) and "other"; self + default filtered out
    expect(optionValues(selects(container)[0])).toEqual(["", "other"]);
  });

  it("writes base_profile_id on change", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({ onWrite });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    fireEvent.change(selects(container)[0], { target: { value: "other" } });
    expect(onWrite).toHaveBeenCalledWith({ path: ["profile_meta", "base_profile_id"], value: "other" });
  });
});

describe("HarnessProfileMeta — runtime profile pin", () => {
  it("lists live profiles for a new pick and writes the field on change", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({ onWrite, profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const sel = selects(container)[1];
    // inherit + live profile (tombstone not pinned → not shown)
    expect(optionValues(sel)).toEqual(["", "rp1"]);
    fireEvent.change(sel, { target: { value: "rp1" } });
    expect(onWrite).toHaveBeenCalledWith({ path: ["profile_meta", "default_runtime_profile_id"], value: "rp1" });
  });

  it("badges a tombstoned (deleted) pinned profile as a disabled option", async () => {
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp2" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const sel = selects(container)[1];
    expect((sel as HTMLSelectElement).value).toBe("rp2");
    // inherit + tombstone (rp2) + live (rp1)
    expect(optionValues(sel)).toEqual(["", "rp2", "rp1"]);
    const tomb = sel.querySelector('option[value="rp2"]') as HTMLOptionElement;
    expect(tomb.disabled).toBe(true);
  });
});

describe("HarnessProfileMeta — model pin", () => {
  it("prepends a pinned model absent from the catalog as a disabled, badged option", async () => {
    const { container } = renderMeta({
      profile: profile({ default_runtime_profile_id: "rp1", default_model: "m-pin" }),
    });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const sel = selects(container)[2];
    expect((sel as HTMLSelectElement).value).toBe("m-pin");
    // pinned-first ordering: m-pin (absent) then catalog m-cat
    expect(optionValues(sel)).toEqual(["", "m-pin", "m-cat"]);
    const pin = sel.querySelector('option[value="m-pin"]') as HTMLOptionElement;
    expect(pin.disabled).toBe(true);
    expect(pin.textContent).toContain("model.notSelectable");
    const live = sel.querySelector('option[value="m-cat"]') as HTMLOptionElement;
    expect(live.disabled).toBe(false);
    expect(live.textContent).not.toContain("model.notSelectable");
  });

  it("writes default_model on change", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({
      onWrite,
      profile: profile({ default_runtime_profile_id: "rp1" }),
    });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    fireEvent.change(selects(container)[2], { target: { value: "m-cat" } });
    expect(onWrite).toHaveBeenCalledWith({ path: ["profile_meta", "default_model"], value: "m-cat" });
  });

  it("offers no catalog models when the catalog is null", async () => {
    applyCfg({ catalog: null });
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    expect(optionValues(selects(container)[2])).toEqual([""]);
  });
});

describe("HarnessProfileMeta — effort pin", () => {
  it("writes default_reasoning_effort on change", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({
      onWrite,
      profile: profile({ default_runtime_profile_id: "rp1" }),
    });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    fireEvent.change(selects(container)[3], { target: { value: "high" } });
    expect(onWrite).toHaveBeenCalledWith({ path: ["profile_meta", "default_reasoning_effort"], value: "high" });
  });

  it("hides efforts when no runtime profile is pinned (pinnedProvider falsy arm)", async () => {
    const { container } = renderMeta({ profile: profile() });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    // no pin → no provider, no efforts → effort select is only inherit
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });

  it("resolves a pinned id absent from the snapshot to null (?? null arm → no efforts)", async () => {
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "ghost" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    // ghost not in snapshot → pinnedRuntimeProfile null → no provider → empty efforts
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });

  it("tolerates a null snapshot (optional-chain arm → no pinned profile, no efforts)", async () => {
    applyCfg({ snapshot: null });
    const { container } = renderMeta({ profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    expect(optionValues(selects(container)[3])).toEqual([""]);
  });
});

describe("HarnessProfileMeta — provisioning prompt", () => {
  it("writes the draft on blur only when it changed", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({
      onWrite,
      profile: profile({ provisioning_prompt: "original" }),
    });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const ta = container.querySelector("textarea.harness-setting-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "edited" } });
    fireEvent.blur(ta);
    expect(onWrite).toHaveBeenCalledWith({ path: ["profile_meta", "provisioning_prompt"], value: "edited" });
  });

  it("does not write on blur when the draft equals the stored prompt", async () => {
    const onWrite = vi.fn();
    const { container } = renderMeta({
      onWrite,
      profile: profile({ provisioning_prompt: "original" }),
    });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const ta = container.querySelector("textarea.harness-setting-input") as HTMLTextAreaElement;
    fireEvent.blur(ta); // unchanged
    expect(onWrite).not.toHaveBeenCalled();
  });

  it("resyncs the draft when the provisioning_prompt prop changes", async () => {
    const { container, rerender } = renderMeta({ profile: profile({ provisioning_prompt: "a" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const ta = () => container.querySelector("textarea.harness-setting-input") as HTMLTextAreaElement;
    fireEvent.change(ta(), { target: { value: "typed" } });
    expect(ta().value).toBe("typed");

    rerender(
      <HarnessProfileMeta
        profile={profile({ provisioning_prompt: "fresh" })}
        profiles={[
          { id: "self", name: "Self" },
          { id: "default", name: "Default" },
          { id: "other", name: "Other" },
        ]}
        disabled={false}
        onWrite={vi.fn()}
      />,
    );
    // useEffect resyncs the draft to the new prop
    await waitFor(() => expect(ta().value).toBe("fresh"));
  });
});

describe("HarnessProfileMeta — disabled state", () => {
  it("disables every input when disabled=true", async () => {
    const { container } = renderMeta({ disabled: true, profile: profile({ default_runtime_profile_id: "rp1" }) });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const all = [...container.querySelectorAll("article.harness-profile-meta .harness-setting-input")] as HTMLElement[];
    expect(all.length).toBeGreaterThan(0);
    expect(all.every((el) => (el as HTMLInputElement).disabled)).toBe(true);
  });

  it("forces the model + effort selects disabled when no profile is pinned", async () => {
    const { container } = renderMeta({ profile: profile() });
    await waitFor(() => expect(trackedFetchMock).toHaveBeenCalled());
    const sels = selects(container);
    // model select is 3rd (index 2); disabled || !pinnedRuntimeProfileId
    expect((sels[2] as HTMLSelectElement).disabled).toBe(true);
  });
});
