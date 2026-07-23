import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExtensionModuleSlot, type ExtensionFrontendModule } from "../src/components/ExtensionSlots";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeModule(overrides: Partial<ExtensionFrontendModule> = {}): ExtensionFrontendModule {
  return {
    extension_id: "ofek-dev.marketplace",
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

function dispatchBridgeMessage(data: unknown, source: Window | null) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", { data, source: source as MessageEventSource | null }),
    );
  });
}

function renderedIframe(): HTMLIFrameElement {
  const iframe = document.querySelector("iframe");
  if (!iframe) throw new Error("iframe not rendered");
  return iframe;
}

describe("extension payment bridge", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const path = String(url);
        if (path.endsWith("/billing/config")) {
          return new Response(
            JSON.stringify({ client_token: "test_client_token", environment: "sandbox" }),
            { status: 200 },
          );
        }
        if (path.endsWith("/billing/checkout")) {
          return new Response(
            JSON.stringify({
              transaction_id: "txn_1",
              product: { name: "Pro", amount: 900, currency: "USD", interval: "month" },
            }),
            { status: 200 },
          );
        }
        if (path.endsWith("/auth/start")) {
          return new Response(JSON.stringify({ login_url: "https://example.com/login", state: "state-1" }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      }),
    );
    window.Paddle = {
      Environment: { set: vi.fn() },
      Initialize: vi.fn(),
      Checkout: { open: vi.fn(), close: vi.fn() },
    } as unknown as typeof window.Paddle;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    delete (window as { Paddle?: unknown }).Paddle;
  });

  it("opens the payment modal only for messages from the module's own iframe", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();

    // Spoof: same payload from a foreign window (the test window) is ignored.
    dispatchBridgeMessage(
      { source: "ba-extension", nonce: "00000000-0000-4000-8000-000000000001", action: "marketplace-purchase", requestId: "r1", productId: "pro" },
      window,
    );
    expect(screen.queryByText("extensionPayment.title")).toBeNull();

    // Legitimate: same payload from the slot's own iframe opens the modal.
    dispatchBridgeMessage(
      { source: "ba-extension", nonce: "00000000-0000-4000-8000-000000000001", action: "marketplace-purchase", requestId: "r2", productId: "pro" },
      iframe.contentWindow,
    );
    await waitFor(() => expect(screen.getByText("extensionPayment.title")).toBeTruthy());
    // Price/name rendered from the server-side checkout response, not the message.
    await waitFor(() => expect(screen.getByText(/Pro/)).toBeTruthy());
  });

  it("ignores purchase requests when the extension lacks the payments permission", () => {
    render(<ExtensionModuleSlot module={makeModule({ payments: false })} />);
    const iframe = renderedIframe();
    dispatchBridgeMessage(
      { source: "ba-extension", nonce: "00000000-0000-4000-8000-000000000001", action: "marketplace-purchase", requestId: "r3", productId: "pro" },
      iframe.contentWindow,
    );
    expect(screen.queryByText("extensionPayment.title")).toBeNull();
  });

  it("initializes the iframe bridge when crypto.randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      randomUUID: undefined,
      getRandomValues: (bytes: Uint8Array) => {
        for (let index = 0; index < bytes.length; index += 1) bytes[index] = index;
        return bytes;
      },
    });

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");
    fireEvent.load(iframe);

    expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "marketplace-auth-init",
        nonce: expect.stringMatching(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
        ),
      }),
      "*",
    );
  });

  it("handles auth-start only from the iframe source and replies over the bridge", async () => {
    const popup = { close: vi.fn(), closed: false } as unknown as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup);
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");

    // Spoofed source: no fetch, no browser open.
    dispatchBridgeMessage(
      { source: "ba-extension", nonce: "00000000-0000-4000-8000-000000000001", action: "marketplace-auth-start", requestId: "a1", provider: "github" },
      window,
    );
    expect(openSpy).not.toHaveBeenCalled();

    dispatchBridgeMessage(
      { source: "ba-extension", nonce: "00000000-0000-4000-8000-000000000001", action: "marketplace-auth-start", requestId: "a2", provider: "github" },
      iframe.contentWindow,
    );
    await waitFor(() => expect(openSpy).toHaveBeenCalledWith("https://example.com/login", "_blank", "popup"));
    await waitFor(() =>
      expect(replySpy).toHaveBeenCalledWith({
        source: "ba-core",
        nonce: "00000000-0000-4000-8000-000000000001",
        requestId: "a2",
        status: "pending",
      }, "*"),
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/ofek-dev.marketplace/backend/auth/start"),
      expect.objectContaining({ method: "POST" }),
    );

    dispatchBridgeMessage(
      { source: "better-agent-marketplace-auth", state: "state-1" },
      popup,
    );
    await waitFor(() =>
      expect(replySpy).toHaveBeenCalledWith({
        source: "ba-core",
        nonce: "00000000-0000-4000-8000-000000000001",
        action: "marketplace-auth-result",
        status: "authenticated",
      }, "*"),
    );
  });

  it("rejects auth requests without the dedicated permission", () => {
    const openSpy = vi.spyOn(window, "open");
    render(<ExtensionModuleSlot module={makeModule({ marketplace_auth: false })} />);
    const iframe = renderedIframe();

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-auth-start",
      requestId: "denied",
      provider: "github",
    }, iframe.contentWindow);

    expect(openSpy).not.toHaveBeenCalled();
  });

  it("proxies only allowlisted marketplace requests from the authenticated iframe bridge", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");
    vi.mocked(fetch).mockClear();

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "allowed",
      path: "/api/extensions/ofek-dev.marketplace/backend/auth/providers",
      method: "GET",
    }, iframe.contentWindow);

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/ofek-dev.marketplace/backend/auth/providers"),
      expect.objectContaining({ method: "GET", credentials: "include" }),
    ));
    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ action: "marketplace-response", requestId: "allowed", ok: true }),
      "*",
    ));

    vi.mocked(fetch).mockClear();
    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "denied",
      path: "/api/config",
      method: "GET",
    }, iframe.contentWindow);

    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ action: "marketplace-response", requestId: "denied", ok: false, error: "marketplace request denied" }),
      "*",
    ));
    expect(fetch).not.toHaveBeenCalled();

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "generic-install-denied",
      path: "/api/extensions/install",
      method: "POST",
      body: { repo_url: "https://attacker.example/repo.git" },
    }, iframe.contentWindow);

    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "generic-install-denied", ok: false, error: "marketplace request denied" }),
      "*",
    ));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("preserves structured backend error details for marketplace requests", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "marketplace login required" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "auth-expired",
      path: "/api/extensions/ofek-dev.marketplace/backend/catalog",
      method: "GET",
    }, iframe.contentWindow);

    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "marketplace-response",
        requestId: "auth-expired",
        ok: false,
        error: "marketplace login required",
      }),
      "*",
    ));
  });

  it("allows a single encoded catalog query and rejects extra query parameters", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");
    vi.mocked(fetch).mockClear();

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "query-allowed",
      path: "/api/extensions/ofek-dev.marketplace/backend/catalog?q=Test%20Ape",
      method: "GET",
    }, iframe.contentWindow);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    vi.mocked(fetch).mockClear();
    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-request",
      requestId: "query-denied",
      path: "/api/extensions/ofek-dev.marketplace/backend/catalog?q=adv&admin=true",
      method: "GET",
    }, iframe.contentWindow);
    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "query-denied", ok: false }),
      "*",
    ));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("previews signed permissions before semantic install and never accepts install coordinates from the iframe", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const path = String(url);
      if (path.endsWith("/api/extensions/marketplace/ofek-dev.adv/preview")) {
        return new Response(JSON.stringify({
          preview_token: "0123456789abcdef0123456789abcdef",
          manifest: {
            id: "ofek-dev.adv",
            name: "ADV",
            version: "1.0.0",
            permissions: { network: true },
          },
        }), { status: 200 });
      }
      if (path.endsWith("/api/extensions/marketplace/ofek-dev.adv/install")) {
        return new Response(JSON.stringify({ extension: { manifest: { id: "ofek-dev.adv" } } }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    });
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-install",
      requestId: "install-1",
      extensionId: "ofek-dev.adv",
      entitlementToken: "entitlement",
      repo_url: "https://attacker.example/ignored.git",
    }, iframe.contentWindow);

    await waitFor(() => expect(screen.getByText("settings.extensionsPermissions")).toBeTruthy());
    expect(screen.getByText("settings.extensionsPermission.network.label")).toBeTruthy();
    fireEvent.click(screen.getByText("app.confirm"));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/marketplace/ofek-dev.adv/install"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          preview_token: "0123456789abcdef0123456789abcdef",
          entitlement_token: "entitlement",
        }),
      }),
    ));
    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "install-1", status: "installed" }),
      "*",
    ));
    expect(JSON.stringify(vi.mocked(fetch).mock.calls)).not.toContain("attacker.example");
  });

  it("coordinates marketplace uninstall through one source-validated core request", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");
    vi.mocked(fetch).mockClear();

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-uninstall",
      requestId: "uninstall-1",
      extensionId: "ofek-dev.adv",
    }, iframe.contentWindow);

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/marketplace/ofek-dev.adv"),
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(JSON.stringify(vi.mocked(fetch).mock.calls)).not.toContain("/backend/extensions/");
    await waitFor(() => expect(replySpy).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "uninstall-1", ok: true }),
      "*",
    ));
  });

  it("reports popup cancellation when focus returns after the popup closes", async () => {
    const popup = { close: vi.fn(), closed: false } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");

    dispatchBridgeMessage({
      source: "ba-extension",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-auth-start",
      requestId: "cancelled",
      provider: "github",
    }, iframe.contentWindow);
    await waitFor(() => expect(window.open).toHaveBeenCalled());
    Object.defineProperty(popup, "closed", { value: true });
    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() => expect(replySpy).toHaveBeenCalledWith({
      source: "ba-core",
      nonce: "00000000-0000-4000-8000-000000000001",
      action: "marketplace-auth-result",
      status: "cancelled",
    }, "*"));
  });
});
