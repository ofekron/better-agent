import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Keep the slice isolated: trackPromise is a passthrough so the real progress
// store (WS extender, retry) doesn't run; fetch is driven per-test.
vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

import { InstallationCapabilities } from "../src/components/InstallationCapabilities";
import { eventBus } from "../src/lib/eventBus";

type Resp = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
};

function resp(opts: { ok?: boolean; status?: number; json?: unknown } = {}): Resp {
  const ok = opts.ok ?? true;
  return {
    ok,
    status: opts.status ?? (ok ? 200 : 400),
    json: async () => opts.json ?? {},
  };
}

function deferred<T = unknown>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

let fetchMock: ReturnType<typeof vi.fn>;
let originalFetch: typeof globalThis.fetch;

/** vitest records every call on fetchMock.mock.calls regardless of the active
 *  impl — safer than a manual log that a mockImplementation override bypasses. */
function patchCalls(): FetchCall[] {
  return fetchMock.mock.calls
    .filter(([url, init]) => {
      const u = url as string;
      const m = (init as RequestInit | undefined)?.method;
      return m === "PATCH" && u.includes("/capabilities/");
    })
    .map(([url, init]) => ({ url: url as string, init: init as RequestInit | undefined }));
}

function patchBody(call: FetchCall): Record<string, unknown> {
  return JSON.parse(call.init?.body as string) as Record<string, unknown>;
}

function profileResponse(capabilities: Record<string, unknown>): Resp {
  return resp({ json: { capabilities } });
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

describe("InstallationCapabilities — load", () => {
  it("renders the setup-required notice when setup_required is true", async () => {
    fetchMock.mockImplementation(async () => resp({ json: { setup_required: true } }));
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilitySetupRequired")).toBeTruthy(),
    );
    expect(container.querySelector(".capability-settings-empty")).toBeTruthy();
    // rows are not rendered in setup-required state
    expect(container.querySelectorAll(".capability-row")).toHaveLength(0);
  });

  it("renders intro + both capability rows on a successful load", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: { enabled: true },
        mobile: { enabled: false },
      }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilityIntro")).toBeTruthy(),
    );
    expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy();
    expect(screen.getByText("settings.capabilityMobile")).toBeTruthy();
    expect(screen.getByText("settings.capabilityIntegrationsHint")).toBeTruthy();
    expect(screen.getByText("settings.capabilityMobileHint")).toBeTruthy();
  });

  it("surfaces an HTTP error message when the load response is not ok", async () => {
    fetchMock.mockImplementation(async () => resp({ ok: false, status: 500 }));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("HTTP 500")).toBeTruthy());
    expect(screen.getByText("HTTP 500").closest(".capability-settings-error")).toBeTruthy();
  });

  it("falls back to 'load failed' for a non-Error rejection", async () => {
    // json() resolves ok but the awaited json() throws a non-Error value.
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

describe("InstallationCapabilities — enable/disable", () => {
  it("PATCHes to enable a capability without the integrations confirm flag (mobile)", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: { enabled: true },
        mobile: { enabled: false },
      }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityMobile")).toBeTruthy());

    const mobileCheckbox = screen
      .getAllByRole("checkbox")[1] as HTMLInputElement;
    expect(mobileCheckbox.checked).toBe(false);
    fireEvent.click(mobileCheckbox);

    await waitFor(() => expect(patchCalls()).toHaveLength(1));
    const call = patchCalls()[0];
    expect(call.url).toContain("/capabilities/mobile");
    expect(patchBody(call)).toEqual({ enabled: true });
    // mobile is non-integrations → confirm is never consulted
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("adds confirm_cancels_extension_work when disabling integrations (confirm accepted)", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: { enabled: true },
        mobile: { enabled: false },
      }),
    );
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
      profileResponse({
        integrations: { enabled: true },
        mobile: { enabled: false },
      }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox); // would disable, but confirm denies

    // Give any accidental PATCH a chance to flush
    await new Promise((r) => setTimeout(r, 0));
    expect(patchCalls()).toHaveLength(0);
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it("disabling a non-integrations capability skips confirm and PATCHes enabled:false", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: { enabled: false },
        mobile: { enabled: true },
      }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityMobile")).toBeTruthy());

    const mobileCheckbox = screen.getAllByRole("checkbox")[1] as HTMLInputElement;
    fireEvent.click(mobileCheckbox); // true -> false; confirmDisable(mobile) early-returns true

    await waitFor(() => expect(patchCalls()).toHaveLength(1));
    const call = patchCalls()[0];
    expect(call.url).toContain("/capabilities/mobile");
    expect(patchBody(call)).toEqual({ enabled: false });
    // non-integrations: confirm is never consulted even when disabling
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("does not add the confirm flag when enabling integrations", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: { enabled: false },
        mobile: { enabled: false },
      }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox); // false -> true

    await waitFor(() => expect(patchCalls()).toHaveLength(1));
    expect(patchBody(patchCalls()[0])).toEqual({ enabled: true });
  });

  it("surfaces a detail message from a failed PATCH", async () => {
    let first = true;
    fetchMock.mockImplementation(async () => {
      if (first) {
        first = false;
        return profileResponse({
          integrations: { enabled: false },
          mobile: { enabled: false },
        });
      }
      return resp({ ok: false, status: 409, json: { detail: "work in progress" } });
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getByText("work in progress")).toBeTruthy());
  });

  it("falls back to HTTP status when a failed PATCH has no detail", async () => {
    let first = true;
    fetchMock.mockImplementation(async () => {
      if (first) {
        first = false;
        return profileResponse({
          integrations: { enabled: false },
          mobile: { enabled: false },
        });
      }
      return resp({ ok: false, status: 400, json: {} });
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getByText("HTTP 400")).toBeTruthy());
  });

  it("tolerates a failed PATCH whose json() rejects (-> null detail)", async () => {
    let first = true;
    fetchMock.mockImplementation(async () => {
      if (first) {
        first = false;
        return profileResponse({
          integrations: { enabled: false },
          mobile: { enabled: false },
        });
      }
      return {
        ok: false,
        status: 502,
        json: async () => {
          throw new Error("bad body");
        },
      };
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getByText("HTTP 502")).toBeTruthy());
  });

  it("falls back to 'update failed' when a successful PATCH body throws a non-Error", async () => {
    let first = true;
    fetchMock.mockImplementation(async () => {
      if (first) {
        first = false;
        return profileResponse({
          integrations: { enabled: false },
          mobile: { enabled: false },
        });
      }
      // ok response, but json() throws a non-Error value
      return {
        ok: true,
        status: 200,
        json: async () => {
          throw "string-boom";
        },
      };
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getByText("update failed")).toBeTruthy());
  });
});

describe("InstallationCapabilities — badges", () => {
  it("disables the checkbox and renders nothing actionable when a capability has no state", async () => {
    fetchMock.mockImplementation(async () => profileResponse({}));
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntro")).toBeTruthy());

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    for (const cb of checkboxes) {
      expect((cb as HTMLInputElement).disabled).toBe(true);
    }
    // No badges rendered when there is no state
    expect(screen.queryByText("settings.capabilitySaving")).toBeNull();
    expect(screen.queryByText("settings.capabilityActive")).toBeNull();
  });

  it("shows the 'needs build' blocked badge when enabled but not provisionable", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: {
          enabled: true,
          provisioned: false,
          self_provisionable: false,
          active: false,
          restart_required: false,
          in_app_restart_supported: false,
        },
        mobile: { enabled: false },
      }),
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
      profileResponse({
        integrations: {
          enabled: true,
          provisioned: true,
          active: false,
          restart_required: true,
          in_app_restart_supported: true,
        },
        mobile: { enabled: false },
      }),
    );
    render(<InstallationCapabilities onRestartRequested={onRestart} />);
    const btn = await screen.findByText("settings.capabilityRestartRequired");
    expect(btn.tagName).toBe("BUTTON");
    fireEvent.click(btn);
    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  it("renders the manual restart span when restart_required but unsupported", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: {
          enabled: true,
          provisioned: true,
          active: false,
          restart_required: true,
          in_app_restart_supported: false,
        },
        mobile: { enabled: false },
      }),
    );
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() =>
      expect(screen.getByText("settings.capabilityRestartManually")).toBeTruthy(),
    );
    const span = container.querySelector(".capability-badge.is-restart");
    expect(span).toBeTruthy();
    expect(span?.tagName).toBe("SPAN");
  });

  it("shows the active badge when provisioned, no restart, active", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({
        integrations: {
          enabled: true,
          provisioned: true,
          active: true,
          restart_required: false,
          in_app_restart_supported: false,
        },
        mobile: { enabled: false },
      }),
    );
    const { container } = render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityActive")).toBeTruthy());
    expect(container.querySelector(".capability-badge.is-active")).toBeTruthy();
  });

  it("shows the busy badge and disables the checkbox while a PATCH is in flight", async () => {
    const pending = deferred<Resp>();
    let first = true;
    fetchMock.mockImplementation(async () => {
      if (first) {
        first = false;
        return profileResponse({
          integrations: { enabled: false, provisioned: false, active: false, restart_required: false, in_app_restart_supported: false },
          mobile: { enabled: false },
        });
      }
      return pending.promise;
    });
    render(<InstallationCapabilities />);
    await waitFor(() => expect(screen.getByText("settings.capabilityIntegrations")).toBeTruthy());

    const checkbox = screen.getAllByRole("checkbox")[0] as HTMLInputElement;
    fireEvent.click(checkbox);

    await waitFor(() => expect(screen.getByText("settings.capabilitySaving")).toBeTruthy());
    expect((checkbox as HTMLInputElement).disabled).toBe(true);

    pending.resolve(profileResponse({
      integrations: { enabled: true, provisioned: true, active: true, restart_required: false, in_app_restart_supported: false },
      mobile: { enabled: false },
    }));
    await waitFor(() => expect(screen.queryByText("settings.capabilitySaving")).toBeNull());
  });
});

describe("InstallationCapabilities — live refresh", () => {
  it("re-fetches on the installation_capabilities_changed fact", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({ integrations: { enabled: false }, mobile: { enabled: false } }),
    );
    render(<InstallationCapabilities />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    eventBus.publish("installation_capabilities_changed", {});
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("stops listening after unmount", async () => {
    fetchMock.mockImplementation(async () =>
      profileResponse({ integrations: { enabled: false }, mobile: { enabled: false } }),
    );
    const { unmount } = render(<InstallationCapabilities />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    eventBus.publish("installation_capabilities_changed", {});
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
