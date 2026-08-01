import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor, act } from "@testing-library/react";

// Back-button hook touches history; neutralize so tests don't fight popstate.
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

import { ExtensionPaymentModal } from "../src/components/ExtensionPaymentModal";
import type { ExtensionPaymentResult } from "../src/components/ExtensionPaymentModal";

// Component's real Paddle.js URL; keep in sync if it changes.
const REAL_PADDLE_SRC = "https://cdn.paddle.com/paddle/v2/paddle.js";

interface FakePaddle {
  Environment: { set: ReturnType<typeof vi.fn> };
  Initialize: ReturnType<typeof vi.fn>;
  Checkout: { open: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> };
  _fire: (name: string) => void;
}

function makePaddle(overrides: Partial<{ closeThrows: boolean }> = {}): FakePaddle {
  let cb: ((e: { name?: string }) => void) | null = null;
  return {
    Environment: { set: vi.fn() },
    Initialize: vi.fn((opts: { eventCallback?: (e: { name?: string }) => void }) => {
      cb = opts.eventCallback ?? null;
    }),
    Checkout: {
      open: vi.fn(),
      close: vi.fn(() => {
        if (overrides.closeThrows) throw new Error("close boom");
      }),
    },
    _fire: (name: string) => {
      if (cb) cb({ name });
    },
  };
}

// happy-dom does not fire synthetic load/error on <script>; intercept
// createElement so the component's addEventListener handlers are capturable.
function captureScriptHandlers() {
  const handlers: { load?: () => void; error?: () => void } = {};
  const realCreate = document.createElement.bind(document);
  vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
    const el = realCreate(tag);
    if (tag.toLowerCase() === "script") {
      el.addEventListener = ((type: string, cb: (e: Event) => void) => {
        if (type === "load") handlers.load = () => cb(new Event("load"));
        if (type === "error") handlers.error = () => cb(new Event("error"));
        return el;
      }) as never;
    }
    return el;
  });
  return handlers;
}

function okJson(json: unknown) {
  return { ok: true, status: 200, json: async () => json, text: async () => "" };
}
function errText(status: number, body: string) {
  return { ok: false, status, json: async () => ({}), text: async () => body };
}

interface BillingCfg {
  config?: unknown;
  checkout?: unknown;
  entitlementSequence?: unknown[];
  configError?: boolean;
  checkoutError?: boolean;
}

function makeBillingFetch(cfg: BillingCfg = {}) {
  const entSeq = cfg.entitlementSequence ?? [{ status: "active", entitlement_token: "etok" }];
  let entIdx = 0;
  return vi.fn(async (url: unknown) => {
    const u = String(url);
    if (u.includes("/billing/config")) {
      if (cfg.configError) return errText(500, "cfg err");
      return okJson(cfg.config ?? { client_token: "tok", environment: "production" });
    }
    if (u.includes("/billing/checkout")) {
      if (cfg.checkoutError) return errText(500, "co err");
      return okJson(
        cfg.checkout ?? {
          transaction_id: "tx1",
          product: { name: "Pro", amount: 500, currency: "usd", interval: "month" },
        },
      );
    }
    if (u.includes("/billing/entitlement/")) {
      return okJson(entSeq[Math.min(entIdx++, entSeq.length - 1)]);
    }
    return okJson({});
  });
}

function deferred<T = unknown>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const PROPS = { extensionId: "ext1", productId: "prod1" };

let originalFetch: typeof globalThis.fetch;
let originalPaddle: unknown;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  originalPaddle = (window as unknown as { Paddle?: unknown }).Paddle;
  delete (window as unknown as { Paddle?: unknown }).Paddle;
  // remove any leftover paddle script tags between tests
  document.querySelectorAll(`script[src="${REAL_PADDLE_SRC}"]`).forEach((s) => s.remove());
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
  if (originalPaddle === undefined) {
    delete (window as unknown as { Paddle?: unknown }).Paddle;
  } else {
    (window as unknown as { Paddle?: unknown }).Paddle = originalPaddle;
  }
  document.querySelectorAll(`script[src="${REAL_PADDLE_SRC}"]`).forEach((s) => s.remove());
});

describe("ExtensionPaymentModal — render gates", () => {
  it("renders nothing when open=false", () => {
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={false} onDone={onDone} />,
    );
    expect(container.querySelector(".modal-overlay")).toBeNull();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("reaches the ready phase, renders checkout info, and opens the inline checkout", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />,
    );
    await waitFor(() => expect(container.querySelector(".extension-paddle-checkout-frame")).toBeTruthy());
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(paddle.Initialize).toHaveBeenCalledWith(expect.objectContaining({ token: "tok" }));
    // production env → Environment.set NOT called
    expect(paddle.Environment.set).not.toHaveBeenCalled();
    // checkout info rendered: name — formatted amount — interval suffix
    expect(container.textContent).toContain("Pro");
    expect(container.textContent).toContain("$5.00");
    expect(container.textContent).toContain("/ month");
  });

  it("sets the sandbox environment when config reports sandbox", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ config: { client_token: "tok", environment: "sandbox" } }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(paddle.Environment.set).toHaveBeenCalledWith("sandbox");
    expect(container.textContent).toContain("$5.00");
  });

  it("omits the interval suffix when the product has none", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      checkout: { transaction_id: "tx1", product: { name: "Pro", amount: 500, currency: "usd", interval: "" } },
    }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(container.textContent).toContain("$5.00");
    expect(container.textContent).not.toContain("/ ");
  });

  it("falls back to a plain amount string when the currency is invalid (formatAmount catch)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      checkout: { transaction_id: "tx1", product: { name: "Pro", amount: 500, currency: "BAD!", interval: "" } },
    }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(container.textContent).toContain("5.00 BAD!");
  });
});

describe("ExtensionPaymentModal — prepare error paths", () => {
  it("surfaces unavailable when the checkout session has no transaction_id", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ checkout: { transaction_id: "", product: {} } }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")).toBeTruthy());
    expect(paddle.Checkout.open).not.toHaveBeenCalled();
  });

  it("defaults a session missing product + transaction_id (nullish coalesce arms) and surfaces unavailable", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ checkout: {} }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")).toBeTruthy());
    expect(paddle.Checkout.open).not.toHaveBeenCalled();
  });

  it("surfaces unavailable when config has no client_token", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ config: { client_token: "", environment: "production" } }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")).toBeTruthy());
    expect(paddle.Checkout.open).not.toHaveBeenCalled();
  });

  it("shows the config error body when /billing/config is not ok", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ configError: true }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")?.textContent).toContain("cfg err"));
  });

  it("shows the checkout error body when /billing/checkout is not ok", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ checkoutError: true }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")?.textContent).toContain("co err"));
  });

  it("defaults missing client_token/environment to '' / production (nullish coalesce arms)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ config: {} }) as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    // client_token defaulted to "" → unavailable
    await waitFor(() => expect(container.querySelector(".setup-error")).toBeTruthy());
    expect(paddle.Checkout.open).not.toHaveBeenCalled();
  });

  it("renders a non-Error thrown value via String(e) in the error phase", async () => {
    const paddle = makePaddle();
    paddle.Checkout.open.mockImplementation(() => {
      throw "boom-string"; // non-Error throw
    });
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(container.querySelector(".setup-error")?.textContent).toContain("boom-string"));
  });
});

describe("ExtensionPaymentModal — Paddle.js loading", () => {
  it("creates a script tag and proceeds once it loads (window.Paddle absent)", async () => {
    const handlers = captureScriptHandlers();
    const paddle = makePaddle();
    globalThis.fetch = makeBillingFetch() as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    // a script was created and appended to <head>
    await waitFor(() => expect(handlers.load).toBeTruthy());
    expect(document.querySelector(`script[src="${REAL_PADDLE_SRC}"]`)).not.toBeNull();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    await act(async () => { handlers.load!(); });
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(container.textContent).toContain("$5.00");
  });

  it("reuses an existing script tag's load event instead of creating a new one", async () => {
    const handlers = captureScriptHandlers();
    const existing = document.createElement("script");
    existing.src = REAL_PADDLE_SRC;
    document.head.appendChild(existing);
    const paddle = makePaddle();
    globalThis.fetch = makeBillingFetch() as never;
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />);
    await waitFor(() => expect(handlers.load).toBeTruthy());
    // no SECOND script created
    expect(document.querySelectorAll(`script[src="${REAL_PADDLE_SRC}"]`)).toHaveLength(1);
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    await act(async () => { handlers.load!(); });
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
  });

  it("enters the error phase when a pre-existing script tag fires error", async () => {
    const handlers = captureScriptHandlers();
    const existing = document.createElement("script");
    existing.src = REAL_PADDLE_SRC;
    document.head.appendChild(existing);
    globalThis.fetch = makeBillingFetch() as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(handlers.error).toBeTruthy());
    await act(async () => { handlers.error!(); });
    await waitFor(() =>
      expect(container.querySelector(".setup-error")?.textContent).toContain("Paddle.js failed to load"),
    );
  });

  it("enters the error phase when the script fails to load", async () => {
    const handlers = captureScriptHandlers();
    globalThis.fetch = makeBillingFetch() as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(handlers.error).toBeTruthy());
    await act(async () => { handlers.error!(); });
    await waitFor(() =>
      expect(container.querySelector(".setup-error")?.textContent).toContain("Paddle.js failed to load"),
    );
  });

  it("throws when the script loaded but window.Paddle is still absent", async () => {
    const handlers = captureScriptHandlers();
    globalThis.fetch = makeBillingFetch() as never;
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(handlers.load).toBeTruthy());
    // load fires but Paddle never appears on window
    await act(async () => { handlers.load!(); });
    await waitFor(() =>
      expect(container.querySelector(".setup-error")?.textContent).toContain("Paddle.js failed to load"),
    );
  });
});

describe("ExtensionPaymentModal — checkout completion + entitlement poll", () => {
  it("completes with active + the entitlement token on checkout.completed (first poll hit)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      entitlementSequence: [{ status: "active", entitlement_token: "etok-1" }],
    }) as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.completed"));
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith({ status: "active", entitlementToken: "etok-1" }),
    );
  });

  it("completes with failed when the entitlement poll reports failed", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      entitlementSequence: [{ status: "failed" }],
    }) as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.completed"));
    await waitFor(() =>
      expect(onDone).toHaveBeenCalledWith({ status: "failed", error: "extensionPayment.failed" }),
    );
  });

  it("ignores a non-completion checkout event (only checkout.completed finalizes)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.closed"));
    // give the (absent) poll a chance to not run
    await Promise.resolve();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("deduplicates a second checkout.completed via the finalizing guard", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ entitlementSequence: [{ status: "active" }] }) as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.completed"));
    act(() => paddle._fire("checkout.completed"));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it("polls past a pending state and then completes active (fake timers)", async () => {
    vi.useFakeTimers();
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      // first response has NO status key → defaults to "pending" (nullish arm)
      entitlementSequence: [{}, { status: "active", entitlement_token: "etok-2" }],
    }) as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    // flush prepare microtasks → ready, eventCallback wired
    await act(async () => { await vi.runAllTicks(); });
    // first poll pending → schedules ENTITLEMENT_POLL_MS, then second → active
    await act(async () => { paddle._fire("checkout.completed"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(onDone).toHaveBeenCalledWith({ status: "active", entitlementToken: "etok-2" });
  });

  it("times out (failed) when entitlement never goes active within the window (fake timers)", async () => {
    vi.useFakeTimers();
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({
      entitlementSequence: [{ status: "pending" }],
    }) as never;
    const onDone = vi.fn();
    render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await act(async () => { await vi.runAllTicks(); });
    await act(async () => { paddle._fire("checkout.completed"); });
    // exhaust the 3-minute polling window
    await act(async () => { await vi.advanceTimersByTimeAsync(3 * 60 * 1000); });
    expect(onDone).toHaveBeenCalledWith({ status: "failed", error: "extensionPayment.timeout" });
  });
});

describe("ExtensionPaymentModal — cancel + cleanup", () => {
  it("reports cancelled when the close button is clicked", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    fireEvent.click(container.querySelector(".modal-close") as Element);
    expect(onDone).toHaveBeenCalledWith({ status: "cancelled" });
  });

  it("reports cancelled on overlay click", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    fireEvent.click(container.querySelector(".modal-overlay") as Element);
    expect(onDone).toHaveBeenCalledWith({ status: "cancelled" });
  });

  it("does NOT cancel when clicking inside the modal content (stopPropagation)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    fireEvent.click(container.querySelector(".modal-content") as Element);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("does not cancel during finalizing (finalizing guard) even via overlay click", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ entitlementSequence: [{ status: "pending" }] }) as never;
    const onDone = vi.fn();
    const { container } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.completed")); // → finalizing
    await Promise.resolve();
    fireEvent.click(container.querySelector(".modal-overlay") as Element);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("closes the Paddle checkout on unmount", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const { unmount } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    unmount();
    expect(paddle.Checkout.close).toHaveBeenCalled();
  });

  it("swallows a throwing Checkout.close during cleanup", async () => {
    const paddle = makePaddle({ closeThrows: true });
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch() as never;
    const { unmount } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    expect(() => unmount()).not.toThrow();
    expect(paddle.Checkout.close).toHaveBeenCalled();
  });
});

describe("ExtensionPaymentModal — cancellation guards (cancelled=true arms)", () => {
  it("skips Initialize if unmounted while loadPaddle is pending (post-loadPaddle guard)", async () => {
    const handlers = captureScriptHandlers();
    const paddle = makePaddle();
    globalThis.fetch = makeBillingFetch() as never;
    const { unmount } = render(<ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />);
    await waitFor(() => expect(handlers.load).toBeTruthy());
    // loadPaddle is now awaiting the script load; unmount flips cancelled
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    unmount();
    await act(async () => { handlers.load!(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // cancelled guard after loadPaddle must skip Initialize / Checkout.open
    expect(paddle.Initialize).not.toHaveBeenCalled();
    expect(paddle.Checkout.open).not.toHaveBeenCalled();
  });

  it("skips state updates if unmounted after the fetches resolve (post-Promise.all guard)", async () => {
    const paddle = makePaddle();
    const configD = deferred();
    const checkoutD = deferred();
    globalThis.fetch = vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.includes("/billing/config")) return configD.promise;
      if (u.includes("/billing/checkout")) return checkoutD.promise;
      return okJson({});
    }) as never;
    const { unmount } = render(<ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />);
    // resolve fetches, but unmount before prepare continues past the await
    configD.resolve(okJson({ client_token: "tok", environment: "production" }));
    checkoutD.resolve(
      okJson({ transaction_id: "tx1", product: { name: "Pro", amount: 500, currency: "usd", interval: "" } }),
    );
    unmount();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    // flush; cancelled guard must skip loadPaddle/Initialize/Checkout.open
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(paddle.Initialize).not.toHaveBeenCalled();
  });

  it("skips setError when unmounted before a rejecting fetch settles (catch guard)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    const configD = deferred();
    globalThis.fetch = vi.fn(async (url: unknown) => {
      if (String(url).includes("/billing/config")) return configD.promise;
      return okJson({});
    }) as never;
    const { container, unmount } = render(
      <ExtensionPaymentModal {...PROPS} open={true} onDone={vi.fn()} />,
    );
    unmount();
    configD.reject(errText(500, "late cfg err"));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // unmounted → no error node rendered
    expect(container.querySelector(".setup-error")).toBeNull();
  });

  it("does not invoke onDone if the entitlement poll resolves after unmount", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    const entD = deferred();
    globalThis.fetch = vi.fn(async (url: unknown) => {
      if (String(url).includes("/billing/config"))
        return okJson({ client_token: "tok", environment: "production" });
      if (String(url).includes("/billing/checkout"))
        return okJson({ transaction_id: "tx1", product: { name: "Pro", amount: 500, currency: "usd", interval: "" } });
      if (String(url).includes("/billing/entitlement/")) return entD.promise;
      return okJson({});
    }) as never;
    const onDone = vi.fn();
    const { unmount } = render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    act(() => paddle._fire("checkout.completed"));
    unmount();
    entD.resolve(okJson({ status: "active", entitlement_token: "etok" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(onDone).not.toHaveBeenCalled();
  });

  it("ignores a checkout.completed event that arrives after unmount (onCheckoutEvent guard)", async () => {
    const paddle = makePaddle();
    (window as unknown as { Paddle?: unknown }).Paddle = paddle;
    globalThis.fetch = makeBillingFetch({ entitlementSequence: [{ status: "active" }] }) as never;
    const onDone = vi.fn();
    const { unmount } = render(<ExtensionPaymentModal {...PROPS} open={true} onDone={onDone} />);
    await waitFor(() => expect(paddle.Checkout.open).toHaveBeenCalled());
    unmount();
    act(() => paddle._fire("checkout.completed"));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(onDone).not.toHaveBeenCalled();
  });
});
