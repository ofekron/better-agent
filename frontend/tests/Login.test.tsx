/* Dedicated coverage for Login.tsx — the password-login + scan-to-login
 * (QR grant) auth surface. The sibling login-qr-fragment.test.tsx only
 * proves the #qr fragment redeem happy path; this file closes the rest:
 * doLogin status branches + token tolerance, QR redeem from ?qr=search
 * and its error arms, and the QR grant mint/render/refresh lifecycle. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { Login } from "../src/components/Login";

const control = vi.hoisted(() => ({
  setStoredToken: vi.fn(),
  setTokens: vi.fn(),
  toDataURL: vi.fn(),
  // Per-test fetch router. Keys are URL suffixes; value returns a Response.
  // A special "__throw" entry throws to simulate a network failure.
  routes: {} as Record<string, () => Response | Promise<Response>>,
  throwSuffixes: new Set<string>(),
}));

vi.mock("../src/bearerAuth", () => ({
  setStoredToken: (...a: unknown[]) => control.setStoredToken(...a),
  setTokens: (...a: unknown[]) => control.setTokens(...a),
}));

vi.mock("../src/components/ChangeServer", () => ({
  ChangeServerButton: () => <div data-testid="change-server" />,
}));

// qrcode is dynamically imported only on the mint path; stub the namespace.
vi.mock("qrcode", () => ({
  toDataURL: (...a: unknown[]) => control.toDataURL(...a),
}));

// setup.ts i18n has empty resources (returns the bare key, no interpolation).
// Surface interpolation args so the {status} arm is observable.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: unknown) => {
      if (typeof opts === "string") return opts;
      if (opts && typeof opts === "object") return key + JSON.stringify(opts);
      return key;
    },
  }),
}));

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Build a fetch router. `login`/`qrRedeem`/`qrGrant` are Response producers
 * or null (→ unmatched → throws, asserting it isn't called). */
function mockFetch(opts: {
  login?: () => Response;
  qrRedeem?: () => Response;
  qrGrant?: () => Response;
}) {
  control.routes = {};
  control.throwSuffixes = new Set();
  if (opts.login) control.routes["/api/auth/login"] = opts.login;
  if (opts.qrRedeem) control.routes["/api/auth/qr_redeem"] = opts.qrRedeem;
  if (opts.qrGrant) control.routes["/api/auth/qr_grant"] = opts.qrGrant;
}

beforeEach(() => {
  control.setStoredToken.mockClear();
  control.setTokens.mockClear();
  control.toDataURL.mockReset();
  control.toDataURL.mockResolvedValue("data:qr-png");
  control.routes = {};
  control.throwSuffixes = new Set();
  window.history.replaceState(null, "", "/");
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    const u = String(url);
    for (const suffix of control.throwSuffixes) {
      if (u.endsWith(suffix)) throw new Error(`network ${suffix}`);
    }
    for (const [suffix, handler] of Object.entries(control.routes)) {
      if (u.endsWith(suffix)) return handler();
    }
    throw new Error(`unexpected fetch ${u}`);
  }) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
});

// Default: qr_grant disabled so doLogin tests aren't polluted by the mint
// lifecycle (which fires on mount when there is no redeem grant).
function renderNoQr() {
  mockFetch({ qrGrant: () => jsonRes({}, 403) });
  return render(<Login onSuccess={vi.fn()} />);
}

describe("Login — password login (doLogin)", () => {
  it("200 + token stores the bearer token and succeeds", async () => {
    const onSuccess = vi.fn();
    mockFetch({
      login: () => jsonRes({ token: "abc" }),
      qrGrant: () => jsonRes({}, 403),
    });
    render(<Login onSuccess={onSuccess} />);
    fireEvent.change(screen.getByText("login.usernameLabel").nextElementSibling!, {
      target: { value: "ofek" },
    });
    fireEvent.change(screen.getByText("login.passwordLabel").nextElementSibling!, {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "login.signIn" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(control.setStoredToken).toHaveBeenCalledWith("abc");
  });

  it("200 with a non-JSON body still succeeds (cookie-only browser path)", async () => {
    const onSuccess = vi.fn();
    mockFetch({
      login: () => new Response("not-json", { status: 200 }),
      qrGrant: () => jsonRes({}, 403),
    });
    render(<Login onSuccess={onSuccess} />);
    fireEvent.change(screen.getByText("login.usernameLabel").nextElementSibling!, {
      target: { value: "ofek" },
    });
    fireEvent.change(screen.getByText("login.passwordLabel").nextElementSibling!, {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "login.signIn" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(control.setStoredToken).not.toHaveBeenCalled();
  });

  it("200 with a JSON body but no token succeeds without storing a token", async () => {
    const onSuccess = vi.fn();
    mockFetch({ login: () => jsonRes({}), qrGrant: () => jsonRes({}, 403) });
    render(<Login onSuccess={onSuccess} />);
    await fillAndSubmit("ofek", "secret");
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(control.setStoredToken).not.toHaveBeenCalled();
  });

  it("429 surfaces the too-many-attempts error", async () => {
    mockFetch({ login: () => jsonRes({}, 429), qrGrant: () => jsonRes({}, 403) });
    render(<Login onSuccess={vi.fn()} />);
    await fillAndSubmit("ofek", "secret");
    expect((await screen.findByRole("alert")).textContent).toBe("login.tooManyAttempts");
  });

  it("401 surfaces the invalid-credentials error", async () => {
    mockFetch({ login: () => jsonRes({}, 401), qrGrant: () => jsonRes({}, 403) });
    render(<Login onSuccess={vi.fn()} />);
    await fillAndSubmit("ofek", "secret");
    expect((await screen.findByRole("alert")).textContent).toBe("login.invalidCredentials");
  });

  it("other status surfaces the unknown-error with the status code", async () => {
    mockFetch({ login: () => jsonRes({}, 500), qrGrant: () => jsonRes({}, 403) });
    render(<Login onSuccess={vi.fn()} />);
    await fillAndSubmit("ofek", "secret");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("login.unknownError");
    expect(alert.textContent).toContain("500");
  });

  it("a fetch throw surfaces the network error", async () => {
    control.routes = {};
    control.throwSuffixes = new Set(["/api/auth/login", "/api/auth/qr_grant"]);
    render(<Login onSuccess={vi.fn()} />);
    await fillAndSubmit("ofek", "secret");
    expect((await screen.findByRole("alert")).textContent).toBe("login.networkError");
  });

  it("submit button is disabled until both fields are filled", () => {
    renderNoQr();
    const submit = screen.getByRole("button", { name: "login.signIn" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByText("login.usernameLabel").nextElementSibling!, {
      target: { value: "ofek" },
    });
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByText("login.passwordLabel").nextElementSibling!, {
      target: { value: "secret" },
    });
    expect(submit.disabled).toBe(false);
  });
});

describe("Login — QR redeem from ?qr=search", () => {
  it("redeems a search grant for tokens and strips it from the URL", async () => {
    const onSuccess = vi.fn();
    window.history.replaceState(null, "", "/?qr=grant-search");
    mockFetch({
      qrRedeem: () => jsonRes({ access_token: "a", refresh_token: "r" }),
    });
    render(<Login onSuccess={onSuccess} />);
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(control.setTokens).toHaveBeenCalledWith("a", "r");
    expect(window.location.search).toBe("");
    // grant is one-time; qr_grant mint must not run while redeeming
    expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(0);
  });

  it("200 with missing tokens still succeeds but does not setTokens", async () => {
    const onSuccess = vi.fn();
    window.history.replaceState(null, "", "/?qr=grant-search");
    mockFetch({ qrRedeem: () => jsonRes({}) });
    render(<Login onSuccess={onSuccess} />);
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(control.setTokens).not.toHaveBeenCalled();
  });

  it("401 surfaces the expired/used-grant message", async () => {
    window.history.replaceState(null, "", "/?qr=grant-search");
    mockFetch({ qrRedeem: () => jsonRes({}, 401) });
    render(<Login onSuccess={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("expired or was already used");
  });

  it("other status surfaces the generic sign-in-failed message", async () => {
    window.history.replaceState(null, "", "/?qr=grant-search");
    mockFetch({ qrRedeem: () => jsonRes({}, 502) });
    render(<Login onSuccess={vi.fn()} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Sign-in failed");
    expect(alert.textContent).toContain("502");
  });

  it("a fetch throw surfaces the network error", async () => {
    window.history.replaceState(null, "", "/?qr=grant-search");
    control.routes = {};
    control.throwSuffixes = new Set(["/api/auth/qr_redeem"]);
    render(<Login onSuccess={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toBe("login.networkError");
  });

  it("does not replay the one-time grant on re-render", async () => {
    const onSuccess = vi.fn();
    window.history.replaceState(null, "", "/?qr=grant-search");
    mockFetch({
      qrRedeem: () => jsonRes({ access_token: "a", refresh_token: "r" }),
    });
    const { rerender } = render(<Login onSuccess={onSuccess} />);
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    rerender(<Login onSuccess={vi.fn()} />);
    rerender(<Login onSuccess={vi.fn()} />);
    await Promise.resolve();
    expect(fetchCallsMatching("/api/auth/qr_redeem")).toHaveLength(1);
  });
});

describe("Login — QR grant mint + render + refresh", () => {
  it("mints and renders the QR image from a successful grant", async () => {
    mockFetch({
      qrGrant: () => jsonRes({ login_url: "https://x/?qr=g", expires_in: 300 }),
    });
    render(<Login onSuccess={vi.fn()} />);
    const img = await screen.findByAltText("One-time login QR");
    expect(img.getAttribute("src")).toBe("data:qr-png");
    expect(control.toDataURL).toHaveBeenCalledWith(
      "https://x/?qr=g",
      expect.objectContaining({ width: 180 }),
    );
  });

  it("non-ok grant response renders no QR", async () => {
    mockFetch({ qrGrant: () => jsonRes({}, 403) });
    render(<Login onSuccess={vi.fn()} />);
    await waitFor(() => expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(1));
    expect(screen.queryByAltText("One-time login QR")).toBeNull();
  });

  it("ok response without login_url renders no QR", async () => {
    mockFetch({ qrGrant: () => jsonRes({ expires_in: 300 }) });
    render(<Login onSuccess={vi.fn()} />);
    await waitFor(() => expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(1));
    expect(screen.queryByAltText("One-time login QR")).toBeNull();
  });

  it("treats a missing expires_in as the default 300s refresh window", async () => {
    // Number(undefined) → NaN → `|| 300` fallback arm
    mockFetch({ qrGrant: () => jsonRes({ login_url: "https://x/?qr=g" }) });
    render(<Login onSuccess={vi.fn()} />);
    const img = await screen.findByAltText("One-time login QR");
    expect(img.getAttribute("src")).toBe("data:qr-png");
  });

  it("aborts the mint if unmounted before the QR resolves (no setState after unmount)", async () => {
    let resolveQr!: (v: string) => void;
    control.toDataURL.mockImplementation(
      () => new Promise<string>((r) => { resolveQr = r; }),
    );
    mockFetch({ qrGrant: () => jsonRes({ login_url: "https://x/?qr=g", expires_in: 300 }) });
    const { unmount } = render(<Login onSuccess={vi.fn()} />);
    await waitFor(() => expect(control.toDataURL).toHaveBeenCalled());
    unmount(); // cleanup flips `alive` to false
    resolveQr("data:late"); // resolve after unmount → `if (!alive) return`
    await Promise.resolve();
    expect(screen.queryByAltText("One-time login QR")).toBeNull();
  });

  it("a mint fetch throw renders no QR (typed login still works)", async () => {
    control.routes = {};
    control.throwSuffixes = new Set(["/api/auth/qr_grant"]);
    render(<Login onSuccess={vi.fn()} />);
    await waitFor(() => expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(1));
    expect(screen.queryByAltText("One-time login QR")).toBeNull();
  });

  it("re-mints before the grant expires via the refresh timer", async () => {
    vi.useFakeTimers();
    mockFetch({
      qrGrant: () => jsonRes({ login_url: "https://x/?qr=g", expires_in: 300 }),
    });
    render(<Login onSuccess={vi.fn()} />);
    // first mint chain settles through microtasks (no real polling under fake timers)
    await vi.advanceTimersByTimeAsync(0);
    expect(control.toDataURL).toHaveBeenCalledTimes(1);
    // expires_in 300 → refresh in (300-30)*1000 = 270000ms
    await vi.advanceTimersByTimeAsync(270000);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(2);
  });

  it("clamps a tiny/missing expires_in to a 30s minimum refresh window", async () => {
    vi.useFakeTimers();
    mockFetch({
      qrGrant: () => jsonRes({ login_url: "https://x/?qr=g", expires_in: 5 }),
    });
    render(<Login onSuccess={vi.fn()} />);
    await vi.advanceTimersByTimeAsync(0);
    expect(control.toDataURL).toHaveBeenCalledTimes(1);
    // (5-30) < 30 → clamped to 30s = 30000ms
    await vi.advanceTimersByTimeAsync(30000);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchCallsMatching("/api/auth/qr_grant")).toHaveLength(2);
  });
});

// --- helpers ---
function fetchCallsMatching(suffix: string): unknown[] {
  return vi
    .mocked(globalThis.fetch)
    .mock.calls.filter(([url]) => String(url).endsWith(suffix));
}

async function fillAndSubmit(user: string, pass: string) {
  fireEvent.change(screen.getByText("login.usernameLabel").nextElementSibling!, {
    target: { value: user },
  });
  fireEvent.change(screen.getByText("login.passwordLabel").nextElementSibling!, {
    target: { value: pass },
  });
  fireEvent.click(screen.getByRole("button", { name: "login.signIn" }));
}
