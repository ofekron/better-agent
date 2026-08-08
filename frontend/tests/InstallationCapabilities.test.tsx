import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

// Keep the slice isolated: trackPromise is a passthrough so the real progress
// store (WS extender, retry) doesn't run; fetch is driven per-test.
vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

// `tests/setup.ts` globally stubs `lib/systemFeedRegistry` so `submitSystemIntent`
// always returns `null` (not-open contract) — this component's mutation
// therefore always exercises its legacy REST fallback in this file (same
// posture every other Package C native-when-open component's dedicated
// test file takes). Overridden here (this file's own `vi.mock` shadows the
// setup file's for this module) ONLY so `subscribeSystemFrames` captures
// its listener for the "live refresh" describe block below to drive
// directly — `submitSystemIntent` stays `null` throughout.
let systemFrameListener: ((frame: unknown) => void) | undefined;
vi.mock("../src/lib/systemFeedRegistry", () => ({
  SYSTEM_FEED_NAMES: [
    "extension_notices", "extension_config", "harness_profiles", "extension_ui",
    "extension_catalog", "marketplace_bridge", "marketplace_intents", "schedules",
    "host_startup_tasks", "installation_capabilities", "machines", "node_registrations",
  ],
  subscribeSystemFrames: (cb: (frame: unknown) => void) => {
    systemFrameListener = cb;
    return () => {
      systemFrameListener = undefined;
    };
  },
  subscribeSystemSocketConnection: () => () => {},
  isSystemSocketOpen: () => false,
  submitSystemIntent: () => null,
  waitForSystemFrame: () => new Promise(() => {}),
  submitSystemIntentAwaitingUpsert: () => null,
}));

import { InstallationCapabilities } from "../src/components/InstallationCapabilities";
import type { InstallationCapabilityWire } from "../src/adapter/wire";

type Resp = {
  ok: boolean;
  status: number;
  statusText?: string;
  json: () => Promise<unknown>;
  text?: () => Promise<string>;
};

function resp(opts: { ok?: boolean; status?: number; json?: unknown } = {}): Resp {
  const ok = opts.ok ?? true;
  return {
    ok,
    status: opts.status ?? (ok ? 200 : 400),
    statusText: "Error",
    json: async () => opts.json ?? {},
    text: async () => "",
  };
}

function deferred<T = unknown>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function capability(
  id: string, overrides: Partial<InstallationCapabilityWire> = {},
): InstallationCapabilityWire {
  return {
    capability_id: id,
    cv: 1,
    enabled: false,
    display: id,
    provisioned: false,
    active: false,
    restart_required: false,
    self_provisionable: false,
    in_app_restart_supported: false,
    ...overrides,
  };
}

/** `GET /api/v2/surface/installation-capabilities`'s `OkListEnvelope`
 * response body. */
function capabilitiesEnvelope(value: InstallationCapabilityWire[]): Resp {
  return resp({ json: { kind: "ok", value, snapshot_identity: { incarnation: 1, render_rev: 1, hist_rev: 1 } } });
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

let fetchMock: ReturnType<typeof vi.fn>;
let originalFetch: typeof globalThis.fetch;

function patchCalls(): FetchCall[] {
  return fetchMock.mock.calls
    .filter(([url, init]) => {
      const u = url as string;
      const m = (init as RequestInit | undefined)?.method;
      return m === "PATCH" && u.includes("/capabilities/");
    })
    .map(([url, init]) => ({ url: url as string, init: init as RequestInit | undefined }));
}

function getCalls(): FetchCall[] {
  return fetchMock.mock.calls
    .filter(([, init]) => !(init as RequestInit | undefined)?.method)
    .map(([url, init]) => ({ url: url as string, init: init as RequestInit | undefined }));
}

function patchBody(call: FetchCall): Record<string, unknown> {
  return JSON.parse(call.init?.body as string) as Record<string, unknown>;
}

beforeEach(() => {
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
  window.confirm = vi.fn().mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
  delete (window as { confirm?: unknown }).confirm;
});

describe("InstallationCapabilities — load (v2)", () => {
  it("issues a v2 GET for the read model", async () => {
    fetchMock.mockImplementation(async () => capabilitiesEnvelope([capability("integrations")]));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(getCalls()).toHaveLength(1));
    expect(getCalls()[0].url).toContain("/api/v2/surface/installation-capabilities");
  });

  it("renders the setup-required notice when the v2 list is empty", async () => {
    fetchMock.mockImplementation(async () => capabilitiesEnvelope([]));
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilitySetupRequired")).toBeTruthy(),
    );
    expect(container.querySelector(".capability-settings-empty")).toBeTruthy();
    expect(container.querySelectorAll(".capability-row")).toHaveLength(0);
  });

  it("renders intro + both capability rows on a successful load", async () => {
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", { enabled: true }),
        capability("mobile", { enabled: false }),
      ]),
    );
    render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilityIntro")).toBeTruthy(),
    );
    expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy();
    expect(screen.getByText("settings.capabilityMobile")).toBeTruthy();
  });

  it("surfaces an HTTP error message when the v2 GET is not ok", async () => {
    fetchMock.mockImplementation(async () => resp({ ok: false, status: 500 }));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText(/HTTP 500/)).toBeTruthy());
  });

  it("falls back to 'load failed' for a non-Error rejection", async () => {
    fetchMock.mockImplementation(async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw "string-boom";
      },
    }));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("load failed")).toBeTruthy());
  });
});

describe("InstallationCapabilities — enable/disable (legacy REST fallback)", () => {
  it("PATCHes to enable a capability without the integrations confirm flag (mobile)", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return resp({ json: {} });
      return capabilitiesEnvelope([
        capability("integrations", { enabled: true }),
        capability("mobile", { enabled: false }),
      ]);
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityMobile")).toBeTruthy());

    const mobileCheckbox = screen.getAllByRole("checkbox")[1] as HTMLInputElement;
    expect(mobileCheckbox.checked).toBe(false);
    fireEvent.click(mobileCheckbox);

    await waitFor(() => expect(patchCalls()).toHaveLength(1));
    const call = patchCalls()[0];
    expect(call.url).toContain("/api/installation-profile/capabilities/mobile");
    expect(patchBody(call)).toEqual({ enabled: true });
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("adds confirm_cancels_extension_work when disabling integrations (confirm accepted)", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return resp({ json: {} });
      return capabilitiesEnvelope([
        capability("integrations", { enabled: true }),
        capability("mobile", { enabled: false }),
      ]);
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox); // checked true -> false

    await waitFor(() => expect(patchCalls()).toHaveLength(1));
    expect(patchBody(patchCalls()[0])).toEqual({
      enabled: false,
      confirm_cancels_extension_work: true,
    });
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it("does not PATCH when disabling integrations is cancelled", async () => {
    (window.confirm as ReturnType<typeof vi.fn>).mockReturnValue(false);
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", { enabled: true }),
        capability("mobile", { enabled: false }),
      ]),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox);

    await new Promise((r) => setTimeout(r, 0));
    expect(patchCalls()).toHaveLength(0);
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it("surfaces a detail message from a failed PATCH", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return resp({ ok: false, status: 409, json: { detail: "work in progress" } });
      }
      return capabilitiesEnvelope([
        capability("integrations", { enabled: false }),
        capability("mobile", { enabled: false }),
      ]);
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getByText("work in progress")).toBeTruthy());
  });

  it("shows the busy badge while the mutation + refetch are in flight", async () => {
    const pendingPatch = deferred<Resp>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return pendingPatch.promise;
      return capabilitiesEnvelope([
        capability("integrations", { enabled: false }),
        capability("mobile", { enabled: false }),
      ]);
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox);

    await waitFor(() => expect(screen.getByText("settings.capabilitySaving")).toBeTruthy());
    expect(checkbox.disabled).toBe(true);

    pendingPatch.resolve(resp({ json: {} }));
    await waitFor(() => expect(screen.queryByText("settings.capabilitySaving")).toBeNull());
  });
});

describe("InstallationCapabilities — badges", () => {
  it("disables the checkbox and renders nothing actionable when a capability is absent from the list", async () => {
    fetchMock.mockImplementation(async () => capabilitiesEnvelope([capability("mobile")]));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntro")).toBeTruthy());

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    // "integrations" is absent from the returned list -> no state -> disabled.
    expect((checkboxes[0] as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByText("settings.capabilityActive")).toBeNull();
  });

  it("shows the 'needs build' blocked badge when enabled but not provisionable", async () => {
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", { enabled: true, provisioned: false, self_provisionable: false }),
        capability("mobile"),
      ]),
    );
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilityNeedsBuild")).toBeTruthy(),
    );
    expect(container.querySelector(".capability-badge.is-blocked")).toBeTruthy();
  });

  it("renders the in-app restart button when restart_required and supported", async () => {
    const onRestart = vi.fn();
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", {
          enabled: true, provisioned: true, restart_required: true, in_app_restart_supported: true,
        }),
        capability("mobile"),
      ]),
    );
    render(<InstallationCapabilities onRestartRequested={onRestart} />);
    const btn = await screen.findByText("settings.capabilityRestartRequired");
    expect(btn.tagName).toBe("BUTTON");
    fireEvent.click(btn);
    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  it("renders the manual restart span when restart_required but unsupported", async () => {
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", {
          enabled: true, provisioned: true, restart_required: true, in_app_restart_supported: false,
        }),
        capability("mobile"),
      ]),
    );
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilityRestartManually")).toBeTruthy(),
    );
    const span = container.querySelector(".capability-badge.is-restart");
    expect(span?.tagName).toBe("SPAN");
  });

  it("shows the active badge when provisioned, no restart, active", async () => {
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([
        capability("integrations", { enabled: true, provisioned: true, active: true }),
        capability("mobile"),
      ]),
    );
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityActive")).toBeTruthy());
    expect(container.querySelector(".capability-badge.is-active")).toBeTruthy();
  });
});

describe("InstallationCapabilities — live refresh (v2 feed)", () => {
  it("applies an installation_capability_changed frame from the system feed", async () => {
    fetchMock.mockImplementation(async () =>
      capabilitiesEnvelope([capability("integrations", { enabled: false }), capability("mobile")]),
    );

    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());
    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    act(() => {
      systemFrameListener?.({
        type: "installation_capability_changed",
        cv: 2,
        capability: capability("integrations", { enabled: true, active: true }),
      });
    });

    await waitFor(() => expect((screen.getAllByRole("checkbox")[0] as HTMLInputElement).checked).toBe(true));
  });
});
