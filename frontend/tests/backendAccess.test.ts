import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestBackend } from "../src/lib/backendAccess";

describe("requestBackend", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  it("returns readable HTTP responses without probing", async () => {
    const response = new Response(null, { status: 403 });
    vi.mocked(fetch).mockResolvedValue(response);

    await expect(requestBackend("https://backend.test", "/api/auth/me")).resolves.toEqual({
      kind: "http_response",
      response,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("reports browser-blocked access when an opaque health probe resolves", async () => {
    const opaque = { type: "opaque" } as Response;
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(opaque);

    await expect(requestBackend("https://backend.test", "/api/auth/me")).resolves.toEqual({
      kind: "browser_access_blocked",
    });
    expect(fetch).toHaveBeenNthCalledWith(2, "https://backend.test/healthz", {
      cache: "no-store",
      credentials: "omit",
      method: "GET",
      mode: "no-cors",
      signal: expect.any(AbortSignal),
    });
  });

  it("reports unreachable when the request and health probe both reject", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));

    const result = await requestBackend("https://backend.test", "/api/auth/me");

    expect(result.kind).toBe("unreachable");
  });

  it("does not probe non-network failures", async () => {
    vi.mocked(fetch).mockRejectedValue(new DOMException("timed out", "TimeoutError"));

    const result = await requestBackend("https://backend.test", "/api/auth/me");

    expect(result.kind).toBe("unreachable");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("coalesces only concurrent probes and never caches settled results", async () => {
    let resolveProbe!: (response: Response) => void;
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("first"))
      .mockRejectedValueOnce(new TypeError("second"))
      .mockReturnValueOnce(new Promise<Response>((resolve) => { resolveProbe = resolve; }));

    const first = requestBackend("https://backend.test", "/api/one");
    const second = requestBackend("https://backend.test", "/api/two");
    await Promise.resolve();
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledTimes(3);

    resolveProbe({ type: "opaque" } as Response);
    await expect(Promise.all([first, second])).resolves.toEqual([
      { kind: "browser_access_blocked" },
      { kind: "browser_access_blocked" },
    ]);

    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("third"))
      .mockRejectedValueOnce(new TypeError("still down"));
    await expect(requestBackend("https://backend.test", "/api/three")).resolves.toMatchObject({
      kind: "unreachable",
    });
    expect(fetch).toHaveBeenCalledTimes(5);
  });

  it("does not let one caller abort cancel a shared probe", async () => {
    let resolveProbe!: (response: Response) => void;
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("first"))
      .mockRejectedValueOnce(new TypeError("second"))
      .mockReturnValueOnce(new Promise<Response>((resolve) => { resolveProbe = resolve; }));
    const firstController = new AbortController();

    const first = requestBackend("https://backend.test", "/api/one", {}, firstController.signal);
    const second = requestBackend("https://backend.test", "/api/two");
    await Promise.resolve();
    await Promise.resolve();
    firstController.abort();
    resolveProbe({ type: "opaque" } as Response);

    await expect(first).resolves.toMatchObject({ kind: "aborted" });
    await expect(second).resolves.toEqual({ kind: "browser_access_blocked" });
  });

  it("preserves caller abort when the shared probe rejects", async () => {
    let rejectProbe!: (error: unknown) => void;
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("request failed"))
      .mockReturnValueOnce(new Promise<Response>((_resolve, reject) => { rejectProbe = reject; }));
    const controller = new AbortController();

    const request = requestBackend("https://backend.test/", "/api/one", {}, controller.signal);
    await Promise.resolve();
    await Promise.resolve();
    controller.abort();
    rejectProbe(new TypeError("probe failed"));

    await expect(request).resolves.toMatchObject({ kind: "aborted" });
  });
});
