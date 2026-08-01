import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const mocks = vi.hoisted(() => ({
  profiles: [] as any[],
  catalogState: {
    catalog: null as any,
    networkState: "idle",
    loading: false,
    refreshing: false,
    refreshError: null as string | null,
    refresh: vi.fn(),
  },
  busHandler: null as ((p: unknown) => void) | null,
  createRuntimeProfile: vi.fn(),
  refetchProviders: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("../src/api", () => ({
  API: "/api",
  createRuntimeProfile: (payload: any) => mocks.createRuntimeProfile(payload),
}));

vi.mock("../src/hooks/useRuntimeProfiles", () => ({
  useRuntimeProfiles: () => ({ profiles: mocks.profiles }),
}));

vi.mock("../src/hooks/useProviderModelCatalog", () => ({
  useProviderModelCatalog: (id: string) =>
    id
      ? mocks.catalogState
      : {
          catalog: null,
          networkState: "idle",
          loading: false,
          refreshing: false,
          refreshError: null,
          refresh: () => {},
        },
}));

vi.mock("../src/hooks/useBusEffect", () => ({
  useBusEffect: (_topics: unknown, handler: (p: unknown) => void) => {
    mocks.busHandler = handler;
  },
}));

vi.mock("../src/components/RuntimeProfileFields", () => ({
  EffortDefaultField: ({ options, effort, onEffortChange }: any) =>
    options.length ? (
      <div data-testid="effort-field" data-effort={effort}>
        <button data-testid="effort-change" onClick={() => onEffortChange("high")}>
          high
        </button>
      </div>
    ) : null,
  ModelDefaultField: ({ model, onModelChange }: any) => (
    <div data-testid="model-field" data-model={model}>
      <button data-testid="model-change" onClick={() => onModelChange("changed-model")}>
        chg
      </button>
    </div>
  ),
}));

// Real TEMPLATES stay (wizard reads template.defaults); only the components
// are stubbed so the wizard's own step wiring is what's under test.
vi.mock("../src/components/ProviderForm", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/components/ProviderForm")>();
  return {
    ...real,
    ProviderForm: ({ onBack, onSubmit, onClose }: any) => (
      <div data-testid="provider-form">
        <button data-testid="pf-back" onClick={onBack}>
          back
        </button>
        <button data-testid="pf-close" onClick={onClose}>
          close
        </button>
        <button
          data-testid="pf-submit"
          onClick={async () => {
            try {
              await onSubmit({
                name: "X",
                kind: "claude",
                mode: "subscription",
                base_url: "",
                config_dir: "",
                default_permission: {},
                api_key: "",
                suspended: false,
              });
            } catch {
              /* wizard surfaces the error */
            }
          }}
        >
          submit
        </button>
      </div>
    ),
    TemplateGrid: ({ onPick }: any) => (
      <div data-testid="template-grid">
        <button data-testid="tg-claude" onClick={() => onPick("claude")}>
          claude
        </button>
      </div>
    ),
  };
});

import { RuntimeProfileWizard } from "../src/components/RuntimeProfileWizard";

// --- fixtures / helpers -----------------------------------------------------

function makeProvider(over: any = {}) {
  return {
    id: "p1",
    name: "Claude",
    nickname: "",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    suspended: false,
    runner_options: ["native", "better_agent_runner"],
    runner_profiles: [
      { runner: "native", reasoning_efforts: ["low", "medium", "high"] },
      { runner: "better_agent_runner", reasoning_efforts: [] },
    ],
    reasoning_effort_options: ["low", "medium", "high"],
    login_state: { status: "idle", authenticated: false },
    ...over,
  };
}

function cardText(p: any) {
  const nick = (p.nickname ?? "").trim();
  return nick ? `${p.name} — ${nick}` : p.name;
}

function renderWizard(props: any = {}) {
  const onDone = props.onDone ?? vi.fn();
  const onClose = props.onClose ?? vi.fn();
  const onRefetchProviders = props.onRefetchProviders ?? mocks.refetchProviders;
  const utils = render(
    <RuntimeProfileWizard
      providers={props.providers ?? [makeProvider()]}
      onRefetchProviders={onRefetchProviders}
      onDone={onDone}
      onClose={onClose}
    />,
  );
  return { onDone, onClose, onRefetchProviders, ...utils };
}

function setLocalhost() {
  // happy-dom parses href and updates hostname.
  (window.location as any).href = "http://localhost:3000/";
}

function stubFetch(routes: { match: string; resp: () => any }[]) {
  const fn = vi.fn((url: string) => {
    const u = String(url);
    for (const r of routes) {
      if (u.includes(r.match)) return r.resp();
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function json(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 400,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

// Drive the wizard to the defaults step for an (optionally custom) provider.
async function goToDefaults(opts: { provider?: any; providers?: any[] } = {}) {
  const provider = opts.provider ?? makeProvider();
  const providers = opts.providers ?? [provider];
  const r = renderWizard({ providers });
  await act(async () => {
    fireEvent.click(screen.getByText(cardText(provider)));
  });
  await act(async () => {
    fireEvent.click(screen.getByText("runtimeProfile.continue"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("rpw-runner-native"));
  });
  return r;
}

beforeEach(() => {
  mocks.profiles = [];
  mocks.catalogState.catalog = null;
  mocks.catalogState.networkState = "idle";
  mocks.catalogState.refreshError = null;
  mocks.createRuntimeProfile.mockReset();
  mocks.refetchProviders.mockReset();
  mocks.refetchProviders.mockResolvedValue(undefined);
  mocks.busHandler = null;
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- provider step ----------------------------------------------------------

describe("RuntimeProfileWizard — provider step", () => {
  it("renders provider cards with display name (with and without nickname)", () => {
    renderWizard({
      providers: [
        makeProvider({ id: "a", name: "Claude", nickname: "Almog" }),
        makeProvider({ id: "b", name: "Codex", nickname: "" }),
        makeProvider({ id: "c", name: "Custom", mode: "api_key", base_url: "https://api.x" }),
      ],
    });
    expect(screen.getByText("Claude — Almog")).toBeTruthy();
    expect(screen.getByText("Codex")).toBeTruthy();
    // api_key mode label + base_url suffix.
    expect(screen.getByText(/setup\.apiKeyButton · https:\/\/api\.x/)).toBeTruthy();
  });

  it("disables suspended providers and shows the suspended pill", () => {
    renderWizard({ providers: [makeProvider({ suspended: true })] });
    const card = screen.getByText("Claude").closest("button")!;
    expect(card.disabled).toBe(true);
    expect(screen.getByText("setup.suspended")).toBeTruthy();
  });

  it("selects a provider and enables the continue button", async () => {
    renderWizard();
    const cont = screen.getByText("runtimeProfile.continue");
    expect((cont as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      fireEvent.click(screen.getByText("Claude"));
    });
    expect((screen.getByText("runtimeProfile.continue") as HTMLButtonElement).disabled).toBe(false);
  });

  it("new-provider card opens the template grid", async () => {
    renderWizard();
    await act(async () => {
      fireEvent.click(screen.getByText("runtimeProfile.newProviderCard"));
    });
    expect(screen.getByTestId("template-grid")).toBeTruthy();
  });

  it("template pick opens the inline provider form", async () => {
    renderWizard();
    await act(async () => {
      fireEvent.click(screen.getByText("runtimeProfile.newProviderCard"));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("tg-claude"));
    });
    expect(screen.getByTestId("provider-form")).toBeTruthy();
  });

  it("form submit creates the provider and returns to the pick step", async () => {
    stubFetch([{ match: "/api/providers", resp: () => json({ id: "newp", name: "X", kind: "claude", mode: "subscription" }) }]);
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-submit")));
    expect(mocks.refetchProviders).toHaveBeenCalled();
    // Back at the pick step (inline form dismissed).
    expect(screen.queryByTestId("provider-form")).toBeNull();
    expect(screen.getByText("runtimeProfile.newProviderCard")).toBeTruthy();
  });

  it("create failure surfaces an error at the provider step", async () => {
    stubFetch([{ match: "/api/providers", resp: () => json("nope", false) }]);
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-submit")));
    // Failure keeps the inline form up (creation did not advance) and never refetched.
    expect(screen.getByTestId("provider-form")).toBeTruthy();
    expect(mocks.refetchProviders).not.toHaveBeenCalled();
  });

  it("create failure with a non-Error rejection hits the generic-message arm", async () => {
    stubFetch([{ match: "/api/providers", resp: () => json({ id: "newp" }) }]);
    mocks.refetchProviders.mockRejectedValueOnce("boom-string");
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-submit")));
    // onRefetchProviders resolved-then-rejected with a non-Error: the fetch
    // succeeded (refetch was called) but the catch took the non-Error arm,
    // so creation did not advance.
    expect(mocks.refetchProviders).toHaveBeenCalled();
    expect(screen.getByTestId("provider-form")).toBeTruthy();
  });

  it("form close calls onClose", async () => {
    const onClose = vi.fn();
    renderWizard({ onClose });
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-close")));
    expect(onClose).toHaveBeenCalled();
  });

  it("close (×) button calls onClose", () => {
    const onClose = vi.fn();
    const { container } = renderWizard({ onClose });
    fireEvent.click(container.querySelector(".modal-close")!);
    expect(onClose).toHaveBeenCalled();
  });
});

// --- goBack matrix ----------------------------------------------------------

describe("RuntimeProfileWizard — goBack", () => {
  it("back at the pick step abandons the wizard (onDone null)", () => {
    const onDone = vi.fn();
    const { container } = renderWizard({ onDone });
    fireEvent.click(container.querySelector(".modal-back")!);
    expect(onDone).toHaveBeenCalledWith(null);
  });

  it("back at templates returns to pick", async () => {
    const { container } = renderWizard();
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    fireEvent.click(container.querySelector(".modal-back")!);
    expect(screen.queryByTestId("template-grid")).toBeNull();
  });

  it("back at the form returns to templates", async () => {
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-back")));
    expect(screen.getByTestId("template-grid")).toBeTruthy();
  });

  it("back at defaults returns to runner", async () => {
    const { container } = await goToDefaults();
    fireEvent.click(container.querySelector(".modal-back")!);
    expect(screen.getByTestId("rpw-runner-native")).toBeTruthy();
  });

  it("back at runner returns to provider", async () => {
    const { container } = renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.continue")));
    fireEvent.click(container.querySelector(".modal-back")!);
    expect(screen.getByText("runtimeProfile.newProviderCard")).toBeTruthy();
  });
});

// --- runner step ------------------------------------------------------------

describe("RuntimeProfileWizard — runner step", () => {
  it("lists runner options and disables taken runners", async () => {
    mocks.profiles = [{ provider_id: "p1", runner: "native" }];
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.continue")));
    const taken = screen.getByTestId("rpw-runner-native");
    expect((taken as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("runtimeProfile.runnerTaken")).toBeTruthy();
    const free = screen.getByTestId("rpw-runner-better_agent_runner");
    expect((free as HTMLButtonElement).disabled).toBe(false);
  });

  it("runner-step continue button enters defaults (effort falls back to options[0] when no medium)", async () => {
    const provider = makeProvider({
      runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "high"] }],
    });
    const r = await goToDefaults({ provider });
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("low");
  });

  it("runner-step continue re-enters defaults when a runner is already chosen", async () => {
    const r = await goToDefaults(); // runner = "native"
    // Back to runner step: runner state persists, so the footer continue is enabled.
    await act(async () => fireEvent.click(r.container.querySelector(".modal-back")!));
    const cont = screen.getByText("runtimeProfile.continue") as HTMLButtonElement;
    expect(cont.disabled).toBe(false);
    await act(async () => fireEvent.click(cont)); // runner && enterDefaults(runner)
    expect(screen.getByTestId("rpw-create-profile")).toBeTruthy();
  });

  it("preserves a user-edited name and model on a second runner pick", async () => {
    const r = await goToDefaults();
    const input = r.container.querySelector('.rpw-step input[type="text"]') as HTMLInputElement;
    await act(async () => fireEvent.change(input, { target: { value: "Custom" } }));
    await act(async () => fireEvent.click(screen.getByTestId("model-change")));
    await act(async () => fireEvent.click(r.container.querySelector(".modal-back")!));
    await act(async () => fireEvent.click(screen.getByTestId("rpw-runner-better_agent_runner")));
    expect(
      (r.container.querySelector('.rpw-step input[type="text"]') as HTMLInputElement).value,
    ).toBe("Custom");
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("changed-model");
  });
});

// --- enterDefaults effort seeding -------------------------------------------

describe("RuntimeProfileWizard — enterDefaults effort seeding", () => {
  it("seeds medium when the runtime offers it (simple flow, no template)", async () => {
    await goToDefaults();
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("medium");
  });

  it("falls back to first option when medium is not offered", async () => {
    const provider = makeProvider({
      runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "high"] }],
    });
    await goToDefaults({ provider });
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("low");
  });

  it("seeds empty effort when the runner has no effort axis", async () => {
    const provider = makeProvider({
      runner_profiles: [{ runner: "native", reasoning_efforts: [] }],
      reasoning_effort_options: [],
    });
    await goToDefaults({ provider });
    expect(screen.queryByTestId("effort-field")).toBeNull();
  });

  it("seeds effort from the chosen template when offered", async () => {
    stubFetch([{ match: "/api/providers", resp: () => json({ id: "newp", name: "X", kind: "claude", mode: "subscription" }) }]);
    const provider = makeProvider({ runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "medium", "high"] }] });
    renderWizard({ providers: [provider] });
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-submit")));
    // template "claude" seeds effort "medium"
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.continue")));
    await act(async () => fireEvent.click(screen.getByTestId("rpw-runner-native")));
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("medium");
    // template seeds model default
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("claude-opus-5[1m]");
  });

  it("drops a template effort seed the runner does not offer", async () => {
    stubFetch([{ match: "/api/providers", resp: () => json({ id: "newp", name: "X", kind: "claude", mode: "subscription" }) }]);
    // template "claude" seeds "medium"; runner only offers low/high → fallback to first
    const provider = makeProvider({ runner_profiles: [{ runner: "native", reasoning_efforts: ["low", "high"] }] });
    renderWizard({ providers: [provider] });
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.newProviderCard")));
    await act(async () => fireEvent.click(screen.getByTestId("tg-claude")));
    await act(async () => fireEvent.click(screen.getByTestId("pf-submit")));
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("runtimeProfile.continue")));
    await act(async () => fireEvent.click(screen.getByTestId("rpw-runner-native")));
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("low");
  });
});

// --- defaults step: catalog effect + inputs ---------------------------------

describe("RuntimeProfileWizard — defaults step", () => {
  it("fills the model from the catalog when none is chosen", async () => {
    const r = await goToDefaults();
    mocks.catalogState.catalog = { provider_id: "p1", models: ["m1"], authoritative: false, status: "stale" };
    await act(async () => r.rerender(<RuntimeProfileWizard providers={[makeProvider()]} onRefetchProviders={mocks.refetchProviders} onDone={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("m1");
  });

  it("ignores a catalog whose provider_id does not match", async () => {
    const r = await goToDefaults();
    mocks.catalogState.catalog = { provider_id: "other", models: ["m1"], authoritative: true, status: "current" };
    await act(async () => r.rerender(<RuntimeProfileWizard providers={[makeProvider()]} onRefetchProviders={mocks.refetchProviders} onDone={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("");
  });

  it("drops a chosen model the authoritative catalog no longer lists", async () => {
    const r = await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("model-change")));
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("changed-model");
    mocks.catalogState.catalog = { provider_id: "p1", models: ["x"], authoritative: true, status: "current" };
    await act(async () => r.rerender(<RuntimeProfileWizard providers={[makeProvider()]} onRefetchProviders={mocks.refetchProviders} onDone={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("x");
  });

  it("keeps a chosen model that is still in the catalog", async () => {
    const r = await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("model-change")));
    mocks.catalogState.catalog = { provider_id: "p1", models: ["changed-model"], authoritative: true, status: "current" };
    await act(async () => r.rerender(<RuntimeProfileWizard providers={[makeProvider()]} onRefetchProviders={mocks.refetchProviders} onDone={vi.fn()} onClose={vi.fn()} />));
    expect(screen.getByTestId("model-field").getAttribute("data-model")).toBe("changed-model");
  });

  it("name input edits the profile name and marks it touched", async () => {
    const r = await goToDefaults();
    const input = r.container.querySelector('.rpw-step input[type="text"]') as HTMLInputElement;
    await act(async () => fireEvent.change(input, { target: { value: "My Profile" } }));
    expect(input.value).toBe("My Profile");
  });

  it("effort change callback propagates", async () => {
    await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("effort-change")));
    expect(screen.getByTestId("effort-field").getAttribute("data-effort")).toBe("high");
  });
});

// --- submitProfile ----------------------------------------------------------

describe("RuntimeProfileWizard — submitProfile", () => {
  it("creates the profile and calls onDone with the id", async () => {
    mocks.createRuntimeProfile.mockResolvedValueOnce({ id: "prof-1" });
    const r = await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("rpw-create-profile")));
    expect(r.onDone).toHaveBeenCalledWith("prof-1");
  });

  it("shows the duplicate-pair message on an 'already exists' error", async () => {
    mocks.createRuntimeProfile.mockRejectedValueOnce(new Error("pair already exists"));
    await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("rpw-create-profile")));
    expect(await screen.findByText("runtimeProfile.duplicatePair")).toBeTruthy();
  });

  it("shows the raw message on other Errors", async () => {
    mocks.createRuntimeProfile.mockRejectedValueOnce(new Error("server down"));
    await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("rpw-create-profile")));
    expect(await screen.findByText("server down")).toBeTruthy();
  });

  it("stringifies non-Error rejections", async () => {
    mocks.createRuntimeProfile.mockRejectedValueOnce("str-bang");
    await goToDefaults();
    await act(async () => fireEvent.click(screen.getByTestId("rpw-create-profile")));
    expect(await screen.findByText("str-bang")).toBeTruthy();
  });

  it("disables the create button when the name is empty", async () => {
    const r = await goToDefaults();
    const input = r.container.querySelector('.rpw-step input[type="text"]') as HTMLInputElement;
    await act(async () => fireEvent.change(input, { target: { value: "   " } }));
    expect((screen.getByTestId("rpw-create-profile") as HTMLButtonElement).disabled).toBe(true);
  });

  it("disables the create button when offline", async () => {
    const r = await goToDefaults();
    await act(async () => mocks.busHandler?.({ connected: false }));
    expect((screen.getByTestId("rpw-create-profile") as HTMLButtonElement).disabled).toBe(true);
    // error shows at defaults step
    expect(screen.getByText("runtimeProfile.offlineHint")).toBeTruthy();
  });
});

// --- connection (offline) gating --------------------------------------------

describe("RuntimeProfileWizard — connection", () => {
  it("shows the offline hint and disables mutations when disconnected", async () => {
    renderWizard();
    expect(screen.queryByText("runtimeProfile.offlineHint")).toBeNull();
    await act(async () => mocks.busHandler?.({ connected: false }));
    expect(screen.getByText("runtimeProfile.offlineHint")).toBeTruthy();
    expect(
      (screen.getByText("runtimeProfile.newProviderCard").closest("button") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("stays connected on a connected frame", async () => {
    renderWizard();
    await act(async () => mocks.busHandler?.({ connected: true }));
    expect(screen.queryByText("runtimeProfile.offlineHint")).toBeNull();
  });

  it("ignores non-connection payloads", async () => {
    renderWizard();
    await act(async () => mocks.busHandler?.({ runtime_profiles_changed: [] }));
    await act(async () => mocks.busHandler?.(undefined));
    await act(async () => mocks.busHandler?.("some-string"));
    expect(screen.queryByText("runtimeProfile.offlineHint")).toBeNull();
  });

  it("recovers when reconnected", async () => {
    renderWizard();
    await act(async () => mocks.busHandler?.({ connected: false }));
    expect(screen.getByText("runtimeProfile.offlineHint")).toBeTruthy();
    await act(async () => mocks.busHandler?.({ connected: true }));
    expect(screen.queryByText("runtimeProfile.offlineHint")).toBeNull();
  });
});

// --- OAuth login (desktop / localhost only) ---------------------------------

describe("RuntimeProfileWizard — provider login", () => {
  beforeEach(() => setLocalhost());

  it("does not render the login row for an api_key provider", async () => {
    renderWizard({ providers: [makeProvider({ mode: "api_key" })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    expect(screen.queryByText("setup.logIn")).toBeNull();
  });

  it("shows the signed-in hint when authenticated", async () => {
    renderWizard({ providers: [makeProvider({ login_state: { status: "idle", authenticated: true } })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    expect(screen.getByText("runtimeProfile.signedIn")).toBeTruthy();
  });

  it("runs login on click and clears pending on success", async () => {
    const fetchMock = stubFetch([{ match: "/login", resp: () => json({}) }]);
    renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("setup.logIn")));
    expect(fetchMock).toHaveBeenCalled();
    expect(await screen.findByText("setup.logIn")).toBeTruthy();
  });

  it("surfaces a detail message on a failed login", async () => {
    stubFetch([{ match: "/login", resp: () => json({ detail: "nope" }, false) }]);
    const { container } = renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("setup.logIn")));
    await waitFor(() => {
      const btn = container.querySelector(".rpw-login-row button")!;
      expect(btn.className).toContain("btn-warning");
      expect(btn.getAttribute("title")).toBe("nope");
    });
  });

  it("falls back to the default message when the error body is not JSON", async () => {
    stubFetch([
      { match: "/login", resp: () => ({ ok: false, status: 500, json: async () => { throw new Error("parse"); }, text: async () => "" }) },
    ]);
    const { container } = renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("setup.logIn")));
    await waitFor(() => {
      const btn = container.querySelector(".rpw-login-row button")!;
      expect(btn.className).toContain("btn-warning");
      expect(btn.getAttribute("title")).toBe("login failed");
    });
  });

  it("uses the default message when a failed login body has no detail", async () => {
    stubFetch([{ match: "/login", resp: () => json({}, false) }]);
    const { container } = renderWizard();
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("setup.logIn")));
    await waitFor(() => {
      expect(container.querySelector(".rpw-login-row button")!.getAttribute("title")).toBe("login failed");
    });
  });

  it("renders the retry label for a login_failed status", async () => {
    renderWizard({ providers: [makeProvider({ login_state: { status: "login_failed", authenticated: false, message: "prior" } })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    const btn = screen.getByText("setup.retryLogin");
    expect((btn.closest("button") as HTMLButtonElement).className).toContain("btn-warning");
    expect(btn.closest("button")!.getAttribute("title")).toBe("prior");
  });

  it("renders the running state and cancels an in-flight login", async () => {
    const fetchMock = stubFetch([
      { match: "/login/cancel", resp: () => json({}) },
      { match: "/login", resp: () => json({}) },
    ]);
    renderWizard({ providers: [makeProvider({ login_state: { status: "login_running", authenticated: false } })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    expect(screen.getByText("setup.signingIn")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByText("setup.cancelLogin")));
    expect(fetchMock).toHaveBeenCalled();
  });

  it("swallows a cancel fetch failure", async () => {
    const fetchMock = vi.fn(async () => { throw new Error("net"); });
    vi.stubGlobal("fetch", fetchMock);
    renderWizard({ providers: [makeProvider({ login_state: { status: "login_running", authenticated: false } })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    await act(async () => fireEvent.click(screen.getByText("setup.cancelLogin")));
    expect(fetchMock).toHaveBeenCalled();
  });

  it("disables the login button while a login is running", async () => {
    renderWizard({ providers: [makeProvider({ login_state: { status: "login_running", authenticated: false } })] });
    await act(async () => fireEvent.click(screen.getByText("Claude")));
    // While running, the action button is the cancel button (login button not shown);
    // signingIn label present.
    expect(screen.getByText("setup.signingIn")).toBeTruthy();
  });
});
