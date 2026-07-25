import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { QuotaIndicator } from "../src/components/QuotaIndicator";
import type { QuotaProviderStatus } from "../src/utils/quotaStatus";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const fallback = (options?.defaultValue as string) ?? key;
      return fallback.replace(/\{\{(\w+)\}\}/g, (_m, name) => String(options?.[name] ?? ""));
    },
  }),
}));

describe("QuotaIndicator distinguishes the no-data cases", () => {
  it("shows progress while the backend is still fetching a first reading", () => {
    // Regression: a `loading` status used to render the same empty state as
    // "no usage", so a pending read was indistinguishable from no quota.
    const status: QuotaProviderStatus = {
      provider: "claude",
      label: "Claude",
      supported: true,
      loading: true,
      windows: [],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.querySelector(".quota-loading")).not.toBeNull();
    expect(container.querySelector(".quota-spinner")).not.toBeNull();
    expect(screen.getByRole("status")).toBeTruthy();
    expect(container.textContent).not.toContain("No usage yet");
  });

  it("names the cause instead of blanking when quota cannot be read", () => {
    const status: QuotaProviderStatus = {
      provider: "claude",
      label: "Claude",
      supported: true,
      error: "credentials_expired",
      windows: [],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.querySelector(".quota-unavailable")).not.toBeNull();
    expect(container.textContent).toContain("sign-in expired");
  });

  it("surfaces an unsupported provider's reason", () => {
    const status: QuotaProviderStatus = {
      provider: "agy",
      label: "Antigravity",
      supported: false,
      reason: "credentials_unavailable",
      windows: [],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.textContent).toContain("credentials not readable");
  });

  it("keeps an unknown error code visible verbatim", () => {
    const status: QuotaProviderStatus = {
      provider: "claude",
      label: "Claude",
      supported: true,
      error: "http_503",
      windows: [],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.textContent).toContain("http_503");
  });

  it("reports the real failure code in a stale row's tooltip", () => {
    // Regression: the tooltip hardcoded error:"" so every stale row read
    // "refresh failing: " with no cause.
    const status: QuotaProviderStatus = {
      provider: "claude",
      label: "Claude",
      supported: true,
      stale: true,
      error: "usage_endpoint_rate_limited",
      windows: [{ key: "five_hour", label: "Session (5h)", used_percent: 40 }],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    const row = container.querySelector(".quota-window");
    expect(row?.getAttribute("title")).toContain("usage_endpoint_rate_limited");
  });

  it("still reports genuinely absent usage", () => {
    const status: QuotaProviderStatus = {
      provider: "claude",
      label: "Claude",
      supported: true,
      windows: [],
    };
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.querySelector(".quota-empty")).not.toBeNull();
    expect(container.querySelector(".quota-loading")).toBeNull();
    expect(container.querySelector(".quota-unavailable")).toBeNull();
  });
});

describe("useQuotaStatus polling", () => {
  const PROVIDER = (id: string) => ({
    id,
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    name: id,
    suspended: false,
  });

  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  const jsonOnce = (providers: Record<string, unknown>) => ({
    ok: true,
    json: async () => ({ providers }),
  });

  it("queries the UNION of every subscriber's providers", async () => {
    // Regression: one module-level entry list meant the LAST subscriber to
    // mount overwrote it, so a narrow subscriber (team mode's manager-capable
    // subset) starved the wider one and its providers rendered as undefined.
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue(jsonOnce({}));

    function Narrow() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    function Wide() {
      useQuotaStatus("http://x", [PROVIDER("a"), PROVIDER("b")] as never);
      return null;
    }

    // Wide mounts FIRST so the narrow subscriber is the last writer — the
    // exact order in which a single shared entry list loses provider "b".
    await act(async () => {
      render(
        <>
          <Wide />
          <Narrow />
        </>,
      );
    });

    const bodies = fetchMock.mock.calls.map(
      (call) => JSON.parse((call[1] as RequestInit).body as string).providers,
    );
    const last = bodies[bodies.length - 1] as { id: string }[];
    expect(last.map((p) => p.id).sort()).toEqual(["a", "b"]);
  });

  it("re-polls a bounded number of times while a provider is loading", async () => {
    // Regression: the poller ignored `loading`, so an already-in-flight
    // backend fetch stayed invisible until the next 5-minute tick.
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    const loading = { provider: "claude", label: "Claude", supported: true, loading: true, windows: [] };
    fetchMock.mockResolvedValue(jsonOnce({ a: loading }));

    function Host() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    const afterFirst = fetchMock.mock.calls.length;
    expect(afterFirst).toBe(1);

    // Each retry tick issues exactly one more request...
    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
    }
    expect(fetchMock.mock.calls.length).toBe(afterFirst + 3);

    // ...and then it STOPS: the retry budget is spent, so a permanently
    // loading backend cannot turn into an endless request loop.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000 * 5);
    });
    expect(fetchMock.mock.calls.length).toBe(afterFirst + 3);
  });

  it("does not re-poll for `refreshing`, which already carries a reading", async () => {
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue(
      jsonOnce({
        a: {
          provider: "claude",
          label: "Claude",
          supported: true,
          stale: true,
          refreshing: true,
          windows: [{ key: "five_hour", label: "S", used_percent: 20 }],
        },
      }),
    );

    function Host() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    expect(fetchMock.mock.calls.length).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000 * 6);
    });
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("reports queried providers as pending before any response lands", async () => {
    // Regression: `cached` started empty, so every provider card rendered a
    // definitive "No usage yet" until the first response — the original
    // symptom, reproduced entirely in the frontend.
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    let never: (v: unknown) => void = () => {};
    fetchMock.mockImplementation(() => new Promise((resolve) => (never = resolve)));

    let seen: Record<string, { loading?: boolean }> = {};
    function Host() {
      seen = useQuotaStatus("http://x", [PROVIDER("a")] as never) as never;
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    expect(seen.a?.loading).toBe(true);
    expect(never).toBeTruthy();
  });

  it("says the usage service is unreachable once the retry budget is spent", async () => {
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue({ ok: false, json: async () => ({}) });

    let seen: Record<string, { error?: string; loading?: boolean }> = {};
    function Host() {
      seen = useQuotaStatus("http://x", [PROVIDER("a")] as never) as never;
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
    }
    expect(seen.a?.error).toBe("extension_unreachable");
    expect(seen.a?.loading).toBeUndefined();
  });

  it("re-polls a refresh that has no numbers behind a stale error", async () => {
    // `refreshing` WITHOUT windows means a read is in flight and the only
    // thing to show is a previous error — the good reading is seconds away,
    // so waiting out the 5-minute tick shows a wrong error meanwhile.
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue(
      jsonOnce({
        a: {
          provider: "claude",
          label: "Claude",
          supported: true,
          error: "http_500",
          refreshing: true,
          windows: [],
        },
      }),
    );
    function Host() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    expect(fetchMock.mock.calls.length).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(fetchMock.mock.calls.length).toBe(2);
  });

  it("coalesces rapid tab-focus refetches", async () => {
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue(jsonOnce({ a: { provider: "claude", label: "Claude", supported: true, windows: [] } }));
    function Host() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    const before = fetchMock.mock.calls.length;
    await act(async () => {
      for (let i = 0; i < 15; i += 1) document.dispatchEvent(new Event("visibilitychange"));
    });
    // All within the coalescing window -> no extra outbound requests.
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it("retries a failed request instead of silently showing nothing", async () => {
    // A 503 (extension disabled/quarantined) used to be swallowed with no
    // retry and no user-visible signal.
    const { useQuotaStatus } = await import("../src/hooks/useQuotaStatus");
    fetchMock.mockResolvedValue({ ok: false, json: async () => ({}) });

    function Host() {
      useQuotaStatus("http://x", [PROVIDER("a")] as never);
      return null;
    }
    await act(async () => {
      render(<Host />);
    });
    expect(fetchMock.mock.calls.length).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(fetchMock.mock.calls.length).toBe(2);
  });
});
