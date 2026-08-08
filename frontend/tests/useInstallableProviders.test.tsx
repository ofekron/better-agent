// @vitest-environment happy-dom
//
// A9 (parity audit): `useInstallableProviders()` must expose an explicit
// loading/error/retry contract instead of leaving consumers to infer state
// from an empty `templates` array (which is ALSO the legitimate steady-
// state for "fetch succeeded, catalog just happens to be empty" — an
// ambiguity a loading/error flag resolves).

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useInstallableProviders } from "../src/hooks/useInstallableProviders";

vi.mock("../src/api", () => ({ API: "https://backend.test" }));

const INSTALLABLE_URL = "https://backend.test/api/v2/surface/providers/installable";

function jsonRes(body: unknown, ok = true, status = ok ? 200 : 500) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  } as Response);
}

function descriptor(kind: string) {
  return {
    kind,
    display: { label: kind, icon_id: kind, config_copy_key: `provider.config_copy.${kind}` },
    form_schema: [],
    defaults: { name: kind, kind, mode: "api_key", base_url: "", config_dir: "", default_model: "", default_reasoning_effort: "" },
    auth_flows: ["api_key"],
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useInstallableProviders — loading/error/retry", () => {
  it("starts loading with an empty template list", async () => {
    let resolveFetch: (v: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { resolveFetch = resolve; })));

    const { result } = renderHook(() => useInstallableProviders());
    expect(result.current.loading).toBe(true);
    expect(result.current.templates).toEqual([]);
    expect(result.current.error).toBeNull();

    resolveFetch(await jsonRes({ value: [descriptor("claude")] }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.templates).toHaveLength(1);
  });

  it("on fetch failure, exposes a non-null error and stops loading, leaving templates empty", async () => {
    vi.stubGlobal("fetch", vi.fn(() => jsonRes("boom", false, 500)));

    const { result } = renderHook(() => useInstallableProviders());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeTruthy();
    expect(result.current.templates).toEqual([]);
  });

  it("retry() re-fetches and can recover from a prior failure", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(await jsonRes("boom", false, 500))
      .mockResolvedValueOnce(await jsonRes({ value: [descriptor("codex")] }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useInstallableProviders());
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.templates).toEqual([]);

    act(() => {
      result.current.retry();
    });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeNull();
    expect(result.current.templates).toHaveLength(1);
    expect(result.current.templates[0].id).toBe("codex");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toBe(INSTALLABLE_URL);
  });

  it("retry() sets loading back to true synchronously", async () => {
    vi.stubGlobal("fetch", vi.fn(() => jsonRes("boom", false, 500)));
    const { result } = renderHook(() => useInstallableProviders());
    await waitFor(() => expect(result.current.error).toBeTruthy());

    let resolveRetry: (v: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { resolveRetry = resolve; })));
    act(() => {
      result.current.retry();
    });
    await waitFor(() => expect(result.current.loading).toBe(true));

    resolveRetry(await jsonRes({ value: [] }));
    await waitFor(() => expect(result.current.loading).toBe(false));
  });
});
