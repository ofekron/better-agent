import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExtensionModuleSlot, type ExtensionFrontendModule } from "../src/components/ExtensionSlots";

/**
 * Drives the ExtensionModuleSlot marketplace message handlers (install /
 * set-enabled / uninstall / purchase / auth) that the payment-bridge and
 * iframe-auth tests only partially reach. Messages are dispatched on window
 * with source === the rendered iframe's contentWindow, matching the
 * onMessage guard.
 */

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const NONCE = "11111111-1111-4111-8111-111111111111";
const VALID_ID = "ofek-dev.marketplace";
const TOKEN_32 = "a".repeat(32);

function makeModule(overrides: Partial<ExtensionFrontendModule> = {}): ExtensionFrontendModule {
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

function dispatchFromIframe(iframe: HTMLIFrameElement, data: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        source: iframe.contentWindow as MessageEventSource | null,
        data: { source: "ba-extension", nonce: NONCE, requestId: "r1", ...data },
      }),
    );
  });
}

/** Fetch router the test rewrites per-case. */
let fetchImpl: (url: string, init?: RequestInit) => Promise<Response>;

beforeEach(() => {
  vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(NONCE);
  fetchImpl = async () => new Response("{}", { status: 200 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => fetchImpl(String(url), init)),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ExtensionModuleSlot marketplace handlers", () => {
  it("rejects an install with an invalid extension id", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: "bad id", entitlementToken: "x" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-install-result", status: "failed", error: "marketplace install denied" }),
        "*",
      );
    });
  });

  it("opens the install modal when the preview is valid, then confirms the install", async () => {
    const calls: string[] = [];
    fetchImpl = async (url) => {
      calls.push(url);
      if (url.endsWith("/preview")) {
        return new Response(
          JSON.stringify({ manifest: { id: VALID_ID, name: "Pro Plan" }, preview_token: TOKEN_32 }),
          { status: 200 },
        );
      }
      if (url.endsWith("/install")) {
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    };

    const { container } = render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: VALID_ID, entitlementToken: "ent" });
    await waitFor(() => expect(screen.getByText("Pro Plan")).toBeTruthy());

    // Confirm via the modal footer's primary (confirm) button.
    const footerButtons = container.querySelectorAll(".modal-footer button");
    const confirmBtn = Array.from(footerButtons).find((b) => !b.classList.contains("btn-secondary")) ?? footerButtons[1];
    fireEvent.click(confirmBtn!);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-install-result", status: "installed" }),
        "*",
      );
    });
    expect(calls.some((u) => u.endsWith("/install"))).toBe(true);
  });

  it("surfaces a failed install when the preview request errors", async () => {
    fetchImpl = async (url) => {
      if (url.endsWith("/preview")) return new Response("preview boom", { status: 500 });
      return new Response("{}", { status: 200 });
    };

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: VALID_ID, entitlementToken: "ent" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-install-result", status: "failed", error: "preview boom" }),
        "*",
      );
    });
  });

  it("rejects a preview whose manifest id does not match", async () => {
    fetchImpl = async (url) => {
      if (url.endsWith("/preview")) {
        return new Response(
          JSON.stringify({ manifest: { id: "someone-else", name: "X" }, preview_token: TOKEN_32 }),
          { status: 200 },
        );
      }
      return new Response("{}", { status: 200 });
    };

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: VALID_ID, entitlementToken: "ent" });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ status: "failed", error: "marketplace preview is invalid" }),
        "*",
      );
    });
  });

  it("cancels a pending install from the modal", async () => {
    fetchImpl = async (url) => {
      if (url.endsWith("/preview")) {
        return new Response(
          JSON.stringify({ manifest: { id: VALID_ID, name: "Pro Plan" }, preview_token: TOKEN_32 }),
          { status: 200 },
        );
      }
      return new Response("{}", { status: 200 });
    };

    const { container } = render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: VALID_ID, entitlementToken: "ent" });
    await waitFor(() => expect(screen.getByText("Pro Plan")).toBeTruthy());

    const cancelBtn = container.querySelector(".modal-footer .btn-secondary") as HTMLElement;
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-install-result", status: "cancelled" }),
        "*",
      );
    });
  });

  it("shows an error when the confirm-install request fails", async () => {
    fetchImpl = async (url) => {
      if (url.endsWith("/preview")) {
        return new Response(
          JSON.stringify({ manifest: { id: VALID_ID, name: "Pro Plan" }, preview_token: TOKEN_32 }),
          { status: 200 },
        );
      }
      if (url.endsWith("/install")) return new Response("install denied", { status: 403 });
      return new Response("{}", { status: 200 });
    };

    const { container } = render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-install", extensionId: VALID_ID, entitlementToken: "ent" });
    await waitFor(() => expect(screen.getByText("Pro Plan")).toBeTruthy());

    const footerButtons = container.querySelectorAll(".modal-footer button");
    const confirmBtn = Array.from(footerButtons).find((b) => !b.classList.contains("btn-secondary")) ?? footerButtons[1];
    fireEvent.click(confirmBtn!);

    await waitFor(() => {
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("install denied");
    });
    // No install-result posted on failure — the modal stays open with the error.
    expect(post).not.toHaveBeenCalledWith(
      expect.objectContaining({ status: "installed" }),
      "*",
    );
  });

  it("rejects set-enabled with a non-boolean enabled flag, then accepts a valid one", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-set-enabled", extensionId: VALID_ID, enabled: "true" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: false, error: "marketplace action denied" }),
        "*",
      );
    });

    fetchImpl = async () => new Response(JSON.stringify({ enabled: true }), { status: 200 });
    dispatchFromIframe(iframe, { action: "marketplace-set-enabled", extensionId: VALID_ID, enabled: true });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: true }),
        "*",
      );
    });
  });

  it("rejects uninstall with an invalid id, then accepts a valid one", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-uninstall", extensionId: "" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: false, error: "marketplace action denied" }),
        "*",
      );
    });

    fetchImpl = async () => new Response(JSON.stringify({ ok: true }), { status: 200 });
    dispatchFromIframe(iframe, { action: "marketplace-uninstall", extensionId: VALID_ID });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: true }),
        "*",
      );
    });
  });

  it("rejects a purchase with a missing product id", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-purchase", productId: "" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ status: "failed", error: "missing product id" }),
        "*",
      );
    });
  });
});

describe("ExtensionModuleSlot marketplace-request proxy", () => {
  it("denies a request whose path is not on the allowlist", async () => {
    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-request", path: "/api/forbidden", method: "GET" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-response", ok: false, error: "marketplace request denied" }),
        "*",
      );
    });
  });

  it("forwards an allowed GET and returns the parsed JSON payload on success", async () => {
    fetchImpl = async () => new Response(JSON.stringify({ items: [1, 2] }), { status: 200 });

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-request", path: "/api/extensions", method: "GET" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-response", ok: true, error: "", payload: { items: [1, 2] } }),
        "*",
      );
    });
  });

  it("surfaces the structured detail field on a failed response", async () => {
    fetchImpl = async () => new Response(JSON.stringify({ detail: "rate limited" }), { status: 429 });

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-request", path: "/api/extensions", method: "GET" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ ok: false, error: "rate limited" }),
        "*",
      );
    });
  });

  it("falls back to the raw text body when the response is not JSON", async () => {
    fetchImpl = async () => new Response("plain failure", { status: 500 });

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-request", path: "/api/extensions", method: "GET" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ ok: false, error: "plain failure", payload: "plain failure" }),
        "*",
      );
    });
  });

  it("reports a network failure when fetch throws", async () => {
    fetchImpl = async () => {
      throw new Error("network down");
    };

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-request", path: "/api/extensions", method: "GET" });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ ok: false, error: "network down" }),
        "*",
      );
    });
  });
});

describe("ExtensionModuleSlot marketplace action failure branches", () => {
  it("surfaces a server error from set-enabled and uninstall", async () => {
    fetchImpl = async () => new Response("enabled boom", { status: 500 });

    render(<ExtensionModuleSlot module={makeModule()} />);
    const iframe = renderedIframe();
    const post = vi.spyOn(iframe.contentWindow!, "postMessage");

    dispatchFromIframe(iframe, { action: "marketplace-set-enabled", extensionId: VALID_ID, enabled: false });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: false, error: "enabled boom" }),
        "*",
      );
    });

    fetchImpl = async () => new Response("uninstall boom", { status: 500 });
    dispatchFromIframe(iframe, { action: "marketplace-uninstall", extensionId: VALID_ID });
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        expect.objectContaining({ action: "marketplace-action-result", ok: false, error: "uninstall boom" }),
        "*",
      );
    });
  });
});
