import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const fetchFn = vi.hoisted(() => ({ current: vi.fn() }));

vi.mock("react-i18next", () => {
  // Keys rendered verbatim; the component never branches on their content.
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock("../src/api", () => ({ API: "http://test" }));

// trackPromise is a passthrough so the real progress store (WS extender,
// retry, op tracking) doesn't run — we only need `{ promise: fn() }`.
vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

import { RecursionGuardsSettings } from "../src/components/RecursionGuardsSettings";

// --- fetch stub -------------------------------------------------------------

const PREFS_URL = "http://test/api/user-prefs";

type Res = { ok: boolean; status: number; json: () => Promise<unknown> };
const res = (ok: boolean, body: unknown, status = ok ? 200 : 500): Res => ({
  ok,
  status,
  json: () => Promise.resolve(body),
});

function installFetch(impl: (url: string, init?: RequestInit) => Promise<Res>) {
  fetchFn.current = vi.fn(impl) as unknown as typeof fetchFn.current;
  globalThis.fetch = ((...args: Parameters<typeof fetch>) =>
    fetchFn.current(...args)) as unknown as typeof fetch;
}

// Default: GET load resolves empty (defaults retained), PATCH resolves ok.
function defaultFetch(): ReturnType<typeof installFetch> {
  return installFetch((_url, init) => {
    if (init?.method === "PATCH") return Promise.resolve(res(true, {}));
    return Promise.resolve(res(true, {}));
  });
}

// GUARDS order in the component: sync_wait_depth_cap(0), session_creation(1),
// session_max_live_descendants(2).
function inputs(container: HTMLElement): HTMLInputElement[] {
  return Array.from(
    container.querySelectorAll<HTMLInputElement>('input[type="number"]'),
  );
}

// --- tests ------------------------------------------------------------------

describe("RecursionGuardsSettings", () => {
  beforeEach(() => {
    defaultFetch();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders all three guards with defaults, labels, and hints", async () => {
    const { container } = render(<RecursionGuardsSettings />);

    await waitFor(() => {
      const ins = inputs(container);
      expect(ins).toHaveLength(3);
      expect(ins.map((i) => i.value)).toEqual(["3", "5", "12"]);
    });

    // Title + hint render via t() keys.
    expect(container.textContent).toContain("settings.recursionGuardsTitle");
    expect(container.textContent).toContain("settings.recursionGuardsHint");

    // Each input is wired to its hint via aria-describedby.
    const ins = inputs(container);
    expect(ins[0].getAttribute("aria-describedby")).toBe(
      "sync_wait_depth_cap-hint",
    );
    expect(container.querySelector("#sync_wait_depth_cap-hint")).not.toBeNull();

    // min/max reflect the GUARDS config.
    expect(ins[0].min).toBe("0");
    expect(ins[0].max).toBe("20");
    expect(ins[2].max).toBe("200");
  });

  it("applies numeric prefs on load and skips non-numeric values", async () => {
    installFetch(() =>
      Promise.resolve(
        res(true, {
          sync_wait_depth_cap: 7,
          session_creation_depth_cap: "bad", // not a number -> ignored
          session_max_live_descendants: null, // not a number -> ignored
        }),
      ),
    );

    const { container } = render(<RecursionGuardsSettings />);

    await waitFor(() => {
      expect(inputs(container).map((i) => i.value)).toEqual(["7", "5", "12"]);
    });
  });

  it("swallows a load failure and keeps defaults", async () => {
    installFetch(() => Promise.reject(new Error("network down")));

    const { container } = render(<RecursionGuardsSettings />);

    // The .catch(() => {}) must keep the component mounted with defaults.
    await waitFor(() => {
      expect(inputs(container).map((i) => i.value)).toEqual(["3", "5", "12"]);
    });
  });

  it("clamps a value below min to min and PATCHes the bounded value", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    installFetch((url, init) => {
      calls.push({ url, init });
      return Promise.resolve(res(true, {}));
    });

    const { container } = render(<RecursionGuardsSettings />);
    const syncWait = () => inputs(container)[0];

    await waitFor(() => expect(syncWait().value).toBe("3"));

    await act(async () => {
      fireEvent.change(syncWait(), { target: { value: "-5" } });
    });
    await act(async () => {
      fireEvent.blur(syncWait());
    });

    // bounded = min(20, max(0, round(-5))) = 0
    expect(syncWait().value).toBe("0");
    const patch = calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.url).toBe(PREFS_URL);
    expect(patch.init.body).toBe(JSON.stringify({ sync_wait_depth_cap: 0 }));
  });

  it("clamps a value above max to max", async () => {
    const calls: { init: RequestInit }[] = [];
    installFetch((_url, init) => {
      calls.push({ init });
      return Promise.resolve(res(true, {}));
    });

    const { container } = render(<RecursionGuardsSettings />);
    const syncWait = () => inputs(container)[0];
    await waitFor(() => expect(syncWait().value).toBe("3"));

    await act(async () => {
      fireEvent.change(syncWait(), { target: { value: "99" } });
    });
    await act(async () => {
      fireEvent.blur(syncWait());
    });

    // bounded = min(20, max(0, 99)) = 20
    expect(syncWait().value).toBe("20");
    const patch = calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.init.body).toBe(JSON.stringify({ sync_wait_depth_cap: 20 }));
  });

  it("rounds a fractional in-range value", async () => {
    const calls: { init: RequestInit }[] = [];
    installFetch((_url, init) => {
      calls.push({ init });
      return Promise.resolve(res(true, {}));
    });

    const { container } = render(<RecursionGuardsSettings />);
    const creation = () => inputs(container)[1]; // max 50
    await waitFor(() => expect(creation().value).toBe("5"));

    await act(async () => {
      fireEvent.change(creation(), { target: { value: "12.7" } });
    });
    await act(async () => {
      fireEvent.blur(creation());
    });

    expect(creation().value).toBe("13");
    const patch = calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.init.body).toBe(
      JSON.stringify({ session_creation_depth_cap: 13 }),
    );
  });

  it("falls back to the guard fallback on non-finite state", async () => {
    const calls: { init: RequestInit }[] = [];
    installFetch((url, init) => {
      if (init?.method === "PATCH") {
        calls.push({ init });
        return Promise.resolve(res(true, {}));
      }
      // The load guard is only `typeof value === "number"`, and
      // typeof NaN === "number", so a NaN prefs value is admitted into state
      // and reaches the non-finite branch of save() on blur.
      return Promise.resolve(res(true, { sync_wait_depth_cap: NaN }));
    });

    const { container } = render(<RecursionGuardsSettings />);
    const syncWait = () => inputs(container)[0]; // fallback 3
    await waitFor(() => expect(syncWait()).toBeDefined());

    await act(async () => {
      fireEvent.blur(syncWait());
    });

    // bounded falls through to guard.fallback (3) for non-finite input.
    expect(syncWait().value).toBe("3");
    const patch = calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.init.body).toBe(JSON.stringify({ sync_wait_depth_cap: 3 }));
  });

  it("updates the displayed value through onChange without saving", async () => {
    const calls: { init: RequestInit }[] = [];
    installFetch((_url, init) => {
      calls.push({ init });
      return Promise.resolve(res(true, {}));
    });

    const { container } = render(<RecursionGuardsSettings />);
    const descendants = () => inputs(container)[2];
    await waitFor(() => expect(descendants().value).toBe("12"));

    await act(async () => {
      fireEvent.change(descendants(), { target: { value: "42" } });
    });

    expect(descendants().value).toBe("42");
    // No PATCH until blur.
    expect(calls.some((c) => c.init?.method === "PATCH")).toBe(false);
  });

  it("disables inputs during save, re-enables after a failed PATCH, and rejects", async () => {
    // Resolve the PATCH on demand so we can observe the disabled-while-pending
    // window, then release it to exercise the !ok throw + finally re-enable.
    let releasePatch: ((r: Res) => void) | null = null;
    installFetch((_url, init) => {
      if (init?.method === "PATCH") {
        return new Promise<Res>((resolve) => {
          releasePatch = resolve;
        });
      }
      return Promise.resolve(res(true, {}));
    });

    // The component fires `void save(...)`; the !ok throw becomes an unhandled
    // rejection. Swallow it here and assert it landed (proves the throw ran).
    const rejections: unknown[] = [];
    const onUnhandled = (r: unknown) => rejections.push(r);
    process.on("unhandledRejection", onUnhandled);

    try {
      const { container } = render(<RecursionGuardsSettings />);
      const syncWait = () => inputs(container)[0];
      await waitFor(() => expect(syncWait().value).toBe("3"));

      await act(async () => {
        fireEvent.change(syncWait(), { target: { value: "7" } });
      });
      await act(async () => {
        fireEvent.blur(syncWait());
      });

      // setSaving(true) rendered: input disabled while PATCH pending.
      await waitFor(() => expect(syncWait().disabled).toBe(true));

      // Release a failing response -> throw -> finally setSaving(false).
      await act(async () => {
        releasePatch!(res(false, {}, 500));
      });

      await waitFor(() => expect(syncWait().disabled).toBe(false));

      // The rejection propagated out of save (HTTP 500 branch).
      await waitFor(() => expect(rejections.length).toBe(1));
      expect((rejections[0] as Error).message).toBe("HTTP 500");
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });
});
