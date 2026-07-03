import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const path = String(url);
        if (path.endsWith("/billing/config")) {
          return new Response(JSON.stringify({ publishable_key: "pk_test" }), { status: 200 });
        }
        if (path.endsWith("/billing/checkout")) {
          return new Response(
            JSON.stringify({
              client_secret: "pi_secret",
              product: { name: "Pro", amount: 900, currency: "usd", interval: "month" },
            }),
            { status: 200 },
          );
        }
        if (path.endsWith("/auth/start")) {
          return new Response(JSON.stringify({ login_url: "https://example.com/login" }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      }),
    );
    window.Stripe = vi.fn(() => ({
      elements: () => ({ create: () => ({ mount: () => undefined, unmount: () => undefined }) }),
      confirmPayment: async () => ({}),
    })) as unknown as typeof window.Stripe;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    delete (window as { Stripe?: unknown }).Stripe;
  });

  it("opens the payment modal only for messages from the module's own iframe", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();

    // Spoof: same payload from a foreign window (the test window) is ignored.
    dispatchBridgeMessage(
      { source: "ba-extension", action: "marketplace-purchase", requestId: "r1", productId: "pro" },
      window,
    );
    expect(screen.queryByText("extensionPayment.title")).toBeNull();

    // Legitimate: same payload from the slot's own iframe opens the modal.
    dispatchBridgeMessage(
      { source: "ba-extension", action: "marketplace-purchase", requestId: "r2", productId: "pro" },
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
      { source: "ba-extension", action: "marketplace-purchase", requestId: "r3", productId: "pro" },
      iframe.contentWindow,
    );
    expect(screen.queryByText("extensionPayment.title")).toBeNull();
  });

  it("handles auth-start only from the iframe source and replies over the bridge", async () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const replySpy = vi.spyOn(iframe.contentWindow as Window, "postMessage");

    // Spoofed source: no fetch, no browser open.
    dispatchBridgeMessage(
      { source: "ba-extension", action: "marketplace-auth-start", requestId: "a1", provider: "github" },
      window,
    );
    expect(openSpy).not.toHaveBeenCalled();

    dispatchBridgeMessage(
      { source: "ba-extension", action: "marketplace-auth-start", requestId: "a2", provider: "github" },
      iframe.contentWindow,
    );
    await waitFor(() => expect(openSpy).toHaveBeenCalledWith("https://example.com/login", "_blank", "noopener,noreferrer"));
    await waitFor(() =>
      expect(replySpy).toHaveBeenCalledWith({ source: "ba-core", requestId: "a2", ok: true }, "*"),
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/ofek-dev.marketplace/backend/auth/start"),
      expect.objectContaining({ method: "POST" }),
    );
  });
});
