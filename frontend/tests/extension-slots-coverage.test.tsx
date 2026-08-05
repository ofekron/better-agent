import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../src/components/extensionModuleLoader", () => ({
  loadExtensionModule: vi.fn(),
}));

import {
  ExtensionCatalogRecovery,
  ExtensionModuleSlot,
  useExtensionFrontendCatalog,
  useExtensionFrontendModules,
  type ExtensionFrontendCatalog,
  type ExtensionFrontendModule,
} from "../src/components/ExtensionSlots";
import { loadExtensionModule } from "../src/components/extensionModuleLoader";
import { eventBus } from "../src/lib/eventBus";

const loadMock = vi.mocked(loadExtensionModule);

const SLOT = "input-overflow-menu";
const MODULE_URL = "/api/extensions/ofek-dev.supervisor/frontend/ui/supervisor-controls.entry.js";

function makeModule(overrides: Partial<ExtensionFrontendModule> = {}): ExtensionFrontendModule {
  return {
    extension_id: "ofek-dev.supervisor",
    extension_name: "Supervisor",
    slot: "test-slot",
    id: "supervisor-controls",
    label: "Supervisor",
    kind: "module",
    module_url: MODULE_URL,
    ...overrides,
  };
}

function entrypointPayload(modules: Array<Partial<ExtensionFrontendModule>> = []) {
  return {
    entrypoints: [
      {
        extension_id: "ofek-dev.supervisor",
        name: "Supervisor",
        frontend_modules: modules.map((m, i) => ({
          slot: m.slot ?? SLOT,
          id: m.id ?? `m${i}`,
          label: m.label ?? `Module ${i}`,
          kind: m.kind ?? "module",
          module_url: m.module_url ?? MODULE_URL,
        })),
      },
    ],
  };
}

function CatalogProbe({ onCatalog }: { onCatalog?: (c: ExtensionFrontendCatalog) => void }) {
  const catalog = useExtensionFrontendCatalog(SLOT);
  onCatalog?.(catalog);
  return (
    <>
      <ExtensionCatalogRecovery catalog={catalog} />
      {catalog.modules.map((m) => <span key={`${m.extension_id}/${m.id}`}>{m.label}</span>)}
    </>
  );
}

function ModulesProbe() {
  const modules = useExtensionFrontendModules(SLOT);
  return <div data-testid="count">{modules.length}</div>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  loadMock.mockReset();
});

describe("useExtensionFrontendCatalog — error/reset branches", () => {
  it("treats a catalog error whose reset guard rejects (no revision) as a no-op reset", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({ detail: { error: "extension_settings_incompatible", reset_available: true } }),
        { status: 409 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    let latest: ExtensionFrontendCatalog | null = null;
    render(<CatalogProbe onCatalog={(c) => { latest = c; }} />);

    await waitFor(() => expect(latest?.error?.resetAvailable).toBe(true));
    expect(latest?.error?.revision).toBe("");

    const before = fetchMock.mock.calls.length;
    await latest!.reset();
    // Guard rejected: no POST to /reset was issued.
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/settings/reset"))).toBe(false);
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it("surfaces a failed reset and shows the reset-failed notice", async () => {
    let catalogRequests = 0;
    const fetchMock = vi.fn(async () => {
      catalogRequests += 1;
      if (catalogRequests === 1) {
        return new Response(
          JSON.stringify({ detail: { error: "extension_settings_incompatible", reset_available: true, found_schema: 1, revision: "r".repeat(64) } }),
          { status: 409 },
        );
      }
      // The reset POST itself fails.
      return new Response("reset boom", { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<CatalogProbe />);
    await waitFor(() => expect(container.querySelector('[role="alert"]')).toBeTruthy());

    fireEvent.click(container.querySelector(".extension-catalog-reset") as HTMLElement);

    await waitFor(() =>
      expect(container.querySelector(".extension-catalog-reset-error")?.textContent).toContain("extensions.resetSettingsFailed"),
    );
  });

  it("falls back to the default catalog error when the error body is not JSON", async () => {
    // Response.json() rejects -> the inline .catch(() => ({})) runs.
    const broken = {
      ok: false,
      status: 502,
      json: async () => { throw new Error("invalid json"); },
    } as Response;
    vi.stubGlobal("fetch", vi.fn(async () => broken));

    let latest: ExtensionFrontendCatalog | null = null;
    render(<CatalogProbe onCatalog={(c) => { latest = c; }} />);

    await waitFor(() => expect(latest?.error).not.toBeNull());
    expect(latest?.error).toEqual({
      code: "extension_catalog_unavailable",
      resetAvailable: false,
      foundSchema: null,
      revision: "",
    });
  });
});

describe("useExtensionFrontendCatalog — event projection", () => {
  it("applies a framed frontend_modules event without re-fetching, and is stable across replays", async () => {
    let fetchCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        fetchCalls += 1;
        return new Response(JSON.stringify(entrypointPayload([{ label: "Alpha" }])), { status: 200 });
      }),
    );

    const { container } = render(<CatalogProbe />);
    await waitFor(() => expect(container.textContent).toContain("Alpha"));
    const fetchAfterMount = fetchCalls;

    // A framed event with entrypoints takes the applyEntrypoints branch (no fetch).
    eventBus.publish("extension.ui.frontend_modules", entrypointPayload([{ label: "Beta" }]));
    await waitFor(() => expect(container.textContent).toContain("Beta"));
    expect(fetchCalls).toBe(fetchAfterMount);

    // Re-publishing identical modules runs the sameModules comparator and keeps identity.
    eventBus.publish("extension.ui.frontend_modules", entrypointPayload([{ label: "Beta" }]));
    await waitFor(() => expect(container.textContent).toContain("Beta"));
    expect(fetchCalls).toBe(fetchAfterMount);
  });

  it("drops entrypoints that lack an extension id or required module fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ entrypoints: [] }), { status: 200 })),
    );

    const { container } = render(<CatalogProbe />);
    await waitFor(() => expect(container.textContent ?? "").not.toContain("Keep"));

    // Framed payload with one valid module plus two invalid shapes.
    eventBus.publish("extension.ui.frontend_modules", {
      entrypoints: [
        { name: "NoId", frontend_modules: [{ slot: SLOT, id: "x", label: "X", module_url: MODULE_URL }] },
        { extension_id: "ofek-dev.supervisor", frontend_modules: [
          { slot: SLOT, id: "no-label", module_url: MODULE_URL }, // missing label
          { slot: SLOT, id: "no-url", label: "NoUrl" }, // missing module_url
          { slot: "other-slot", id: "wrong-slot", label: "Wrong", module_url: MODULE_URL }, // wrong slot
          { slot: SLOT, id: "keep", label: "Keep", module_url: MODULE_URL }, // valid
        ] },
      ],
    });

    await waitFor(() => expect(container.textContent).toContain("Keep"));
    expect(container.textContent).not.toContain("NoUrl");
    expect(container.textContent).not.toContain("Wrong");
  });
});

describe("useExtensionFrontendModules", () => {
  it("returns just the module array from the catalog", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(entrypointPayload([{ label: "A" }, { label: "B" }])), { status: 200 })),
    );

    const { container } = render(<ModulesProbe />);
    await waitFor(() => expect(container.querySelector("[data-testid='count']")?.textContent).toBe("2"));
  });
});

describe("ExtensionModuleSlot — module mount lifecycle", () => {
  it("cleans up an object-shaped mount cleanup ({ unmount }) on unmount", async () => {
    const unmountSpy = vi.fn();
    loadMock.mockResolvedValue({ mount: async () => ({ unmount: unmountSpy }) });

    const { unmount } = render(<ExtensionModuleSlot module={makeModule()} />);
    await waitFor(() => expect(loadMock).toHaveBeenCalled());

    unmount();
    expect(unmountSpy).toHaveBeenCalledTimes(1);
  });

  it("aborts a Component mount that resolves after the slot unmounts", async () => {
    const componentSpy = vi.fn(() => null);
    let resolveLoad!: (mod: unknown) => void;
    loadMock.mockReturnValue(new Promise((resolve) => { resolveLoad = resolve; }));

    const { unmount } = render(<ExtensionModuleSlot module={makeModule()} />);

    // Unmount while the dynamic import is still pending (cancels the mount).
    unmount();
    resolveLoad({ Component: componentSpy });

    await new Promise((r) => setTimeout(r, 0));
    expect(componentSpy).not.toHaveBeenCalled();
  });

  it("cleans up a mount-function result that resolves after the slot unmounts", async () => {
    const cleanupSpy = vi.fn();
    let resolveMount!: (c: unknown) => void;
    loadMock.mockResolvedValue({
      mount: () => new Promise((resolve) => { resolveMount = resolve; }),
    });

    const { unmount } = render(<ExtensionModuleSlot module={makeModule()} />);
    await waitFor(() => expect(loadMock).toHaveBeenCalled());

    unmount();
    resolveMount(cleanupSpy);

    await new Promise((r) => setTimeout(r, 0));
    expect(cleanupSpy).toHaveBeenCalledTimes(1);
  });

  it("errors when a module exports neither Component nor mount", async () => {
    loadMock.mockResolvedValue({});

    const { container } = render(<ExtensionModuleSlot module={makeModule()} />);
    await waitFor(() =>
      expect(container.querySelector(".setup-error")?.textContent).toContain("has no mount export"),
    );
  });

  it("errors when the dynamic import rejects", async () => {
    loadMock.mockRejectedValue(new Error("import blew up"));

    const { container } = render(<ExtensionModuleSlot module={makeModule()} />);
    await waitFor(() =>
      expect(container.querySelector(".setup-error")?.textContent).toContain("import blew up"),
    );
  });
});

const NONCE = "22222222-2222-4222-8222-222222222222";
const VALID_ID = "ofek-dev.marketplace";

function iframeModule(overrides: Partial<ExtensionFrontendModule> = {}): ExtensionFrontendModule {
  return {
    extension_id: VALID_ID,
    extension_name: "Marketplace",
    slot: "settings",
    id: "marketplace",
    label: "Marketplace",
    kind: "iframe",
    module_url: "/api/extensions/ofek-dev.marketplace/frontend/ui/index.html",
    payments: true,
    marketplace_auth: true,
    ...overrides,
  };
}

function renderedIframe(): HTMLIFrameElement {
  const iframe = document.querySelector("iframe");
  if (!iframe) throw new Error("iframe not rendered");
  return iframe;
}

function postFromIframe(iframe: HTMLIFrameElement, data: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        source: iframe.contentWindow as MessageEventSource | null,
        data,
      }),
    );
  });
}

describe("ExtensionModuleSlot — iframe message guards", () => {
  beforeEach(() => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(NONCE);
  });

  it("ignores extension messages that do not match the bridge nonce", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, {
      source: "ba-extension",
      nonce: "deadbeef",
      requestId: "r1",
      action: "marketplace-purchase",
      productId: "pro",
    });

    await new Promise((r) => setTimeout(r, 0));
    expect(post).not.toHaveBeenCalled();
  });

  it("ignores auth-complete messages with the wrong state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith("/auth/start")) {
          return new Response(JSON.stringify({ login_url: "https://example.com/login", state: "state-good" }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      }),
    );
    const popup = { close: vi.fn(), closed: false } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);

    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    // Start auth so the popup + expected state are recorded.
    postFromIframe(iframe, { source: "ba-extension", nonce: NONCE, requestId: "r1", action: "marketplace-auth-start", provider: "github" });
    await waitFor(() => expect(window.open).toHaveBeenCalled());

    // Auth-complete with a mismatched state is dropped.
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          source: popup as MessageEventSource,
          data: { source: "better-agent-marketplace-auth", state: "wrong" },
        }),
      );
    });

    await new Promise((r) => setTimeout(r, 0));
    // The authenticated result must not have been posted for a mismatched state.
    expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ status: "authenticated" }), "*");
  });

  it("does not report cancellation while the auth popup is still open", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith("/auth/start")) {
          return new Response(JSON.stringify({ login_url: "https://example.com/login", state: "s1" }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      }),
    );
    const popup = { close: vi.fn(), closed: false } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);

    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, { source: "ba-extension", nonce: NONCE, requestId: "r1", action: "marketplace-auth-start", provider: "github" });
    await waitFor(() => expect(window.open).toHaveBeenCalled());

    act(() => { window.dispatchEvent(new Event("focus")); });

    await new Promise((r) => setTimeout(r, 0));
    // Popup still open → focus must not report cancellation.
    expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ status: "cancelled" }), "*");
  });
});

describe("ExtensionModuleSlot — payment completion (onPaymentDone)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(NONCE);
    window.Paddle = {
      Environment: { set: vi.fn() },
      Initialize: vi.fn(),
      Checkout: { open: vi.fn(), close: vi.fn() },
    } as unknown as typeof window.Paddle;
  });
  afterEach(() => {
    delete (window as { Paddle?: unknown }).Paddle;
  });

  it("posts the cancelled payment result to the iframe when the modal is dismissed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const p = String(url);
        if (p.endsWith("/billing/config")) {
          return new Response(JSON.stringify({ client_token: "tok", environment: "sandbox" }), { status: 200 });
        }
        if (p.endsWith("/billing/checkout")) {
          return new Response(
            JSON.stringify({ transaction_id: "txn_1", product: { name: "Pro", amount: 900, currency: "USD", interval: "month" } }),
            { status: 200 },
          );
        }
        return new Response("{}", { status: 200 });
      }),
    );

    const { container } = render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, {
      source: "ba-extension",
      nonce: NONCE,
      requestId: "pay1",
      action: "marketplace-purchase",
      productId: "pro",
    });

    // Wait for the modal to reach the ready phase (Paddle initialized).
    await waitFor(() => expect(window.Paddle!.Initialize).toHaveBeenCalled());

    // Dismiss via the close button → cancel → onDone(cancelled) → onPaymentDone.
    fireEvent.click(container.querySelector(".modal-close") as HTMLElement);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ requestId: "pay1", status: "cancelled", entitlementToken: "", error: "" }),
        "*",
      );
    });
    // Payment request cleared → the modal unmounts.
    expect(container.querySelector(".modal-close")).toBeNull();
  });
});

describe("ExtensionModuleSlot — auth-start failure branches", () => {
  beforeEach(() => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(NONCE);
  });

  it("reports a server error from auth/start", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith("/auth/start")) return new Response("auth boom", { status: 500 });
        return new Response("{}", { status: 200 });
      }),
    );
    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, { source: "ba-extension", nonce: NONCE, requestId: "r1", action: "marketplace-auth-start", provider: "github" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(expect.objectContaining({ ok: false, error: "auth boom" }), "*");
    });
  });

  it("reports a malformed auth/start response (missing login state)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith("/auth/start")) return new Response(JSON.stringify({ login_url: "https://example.com/login" }), { status: 200 });
        return new Response("{}", { status: 200 });
      }),
    );
    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, { source: "ba-extension", nonce: NONCE, requestId: "r1", action: "marketplace-auth-start", provider: "github" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(expect.objectContaining({ ok: false, error: "missing login state" }), "*");
    });
  });

  it("reports a blocked sign-in popup", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith("/auth/start")) return new Response(JSON.stringify({ login_url: "https://example.com/login", state: "s1" }), { status: 200 });
        return new Response("{}", { status: 200 });
      }),
    );
    vi.spyOn(window, "open").mockReturnValue(null);

    render(<ExtensionModuleSlot module={iframeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    postFromIframe(iframe, { source: "ba-extension", nonce: NONCE, requestId: "r1", action: "marketplace-auth-start", provider: "github" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(expect.objectContaining({ ok: false, error: "sign-in popup was blocked" }), "*");
    });
  });
});
