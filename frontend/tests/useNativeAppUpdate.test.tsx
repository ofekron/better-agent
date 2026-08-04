// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useNativeAppUpdate } from "../src/hooks/useNativeAppUpdate";

// Hoisted mutable state read by the hoisted mock factories (mocks are set up
// before the test body runs, so any state they read at call time must live
// here, not in a plain `let`).
const mocks = vi.hoisted(() => ({
  native: true,
  app: {
    getInfo: vi.fn(),
    addListener: vi.fn(),
  },
  remove: vi.fn(),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => mocks.native },
}));

vi.mock("@capacitor/app", () => ({
  App: mocks.app,
}));

vi.mock("../src/api", () => ({ API: "https://backend.test" }));

const DISMISS_KEY = "bc_update_dismissed_code";
const STATUS_URL = "https://backend.test/api/mobile/status";

let activeListener: ((s: { isActive: boolean }) => void) | null = null;

function statusResponse(body: unknown, ok = true) {
  return {
    ok,
    json: async () => body,
  };
}

function stubFetch(impl: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", impl);
}

beforeEach(() => {
  mocks.native = true;
  mocks.app.getInfo.mockResolvedValue({ build: "1" });
  mocks.app.addListener.mockImplementation(
    (_event: string, cb: (s: { isActive: boolean }) => void) => {
      activeListener = cb;
      return Promise.resolve({ remove: mocks.remove });
    },
  );
  mocks.app.getInfo.mockClear();
  mocks.app.addListener.mockClear();
  mocks.remove.mockClear();
  localStorage.clear();
  activeListener = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
  activeListener = null;
});

describe("useNativeAppUpdate", () => {
  it("is a no-op on web (never calls native APIs or the network)", async () => {
    mocks.native = false;
    const fetchSpy = vi.fn();
    stubFetch(fetchSpy);

    const { result } = renderHook(() => useNativeAppUpdate());

    expect(result.current.update).toBeNull();
    expect(mocks.app.getInfo).not.toHaveBeenCalled();
    expect(mocks.app.addListener).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("reports an update when the backend stages a newer build", async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValue(
        statusResponse({ version_code: 5, version_name: "1.5.0" }),
      );
    stubFetch(fetchSpy);

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(result.current.update).not.toBeNull());
    expect(result.current.update).toEqual({ code: 5, versionName: "1.5.0" });
    expect(mocks.app.getInfo).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith(STATUS_URL, {
      credentials: "include",
    });
  });

  it("stays silent when the running build is already current", async () => {
    mocks.app.getInfo.mockResolvedValue({ build: "10" });
    stubFetch(
      vi.fn().mockResolvedValue(statusResponse({ version_code: 5 })),
    );

    const { result } = renderHook(() => useNativeAppUpdate());

    // Give the async check a chance to settle before asserting absence.
    await waitFor(() => expect(mocks.app.getInfo).toHaveBeenCalled());
    expect(result.current.update).toBeNull();
  });

  it("treats a missing/zero server version_code as 'no update'", async () => {
    stubFetch(vi.fn().mockResolvedValue(statusResponse({})));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(mocks.app.getInfo).toHaveBeenCalled());
    expect(result.current.update).toBeNull();
  });

  it("ignores a non-ok status response without throwing", async () => {
    stubFetch(vi.fn().mockResolvedValue(statusResponse({}, false)));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(mocks.app.getInfo).toHaveBeenCalled());
    expect(result.current.update).toBeNull();
  });

  it("swallows network errors and leaves state unchanged", async () => {
    stubFetch(vi.fn().mockRejectedValue(new Error("offline")));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(mocks.app.getInfo).toHaveBeenCalled());
    expect(result.current.update).toBeNull();
  });

  it("parses a non-numeric build as 0 so any staged build wins", async () => {
    mocks.app.getInfo.mockResolvedValue({ build: "abc" });
    stubFetch(vi.fn().mockResolvedValue(statusResponse({ version_code: 1 })));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(result.current.update).not.toBeNull());
    expect(result.current.update).toEqual({ code: 1, versionName: null });
  });

  it("honours a previously dismissed version and resurfaces a newer one", async () => {
    localStorage.setItem(DISMISS_KEY, "5");
    stubFetch(vi.fn().mockResolvedValue(statusResponse({ version_code: 6 })));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(result.current.update).not.toBeNull());
    expect(result.current.update).toEqual({ code: 6, versionName: null });
  });

  it("suppresses a re-staged equal dismissed code", async () => {
    localStorage.setItem(DISMISS_KEY, "5");
    stubFetch(vi.fn().mockResolvedValue(statusResponse({ version_code: 5 })));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(mocks.app.getInfo).toHaveBeenCalled());
    expect(result.current.update).toBeNull();
  });

  it("treats a non-numeric dismissed code as 0", async () => {
    localStorage.setItem(DISMISS_KEY, "not-a-number");
    stubFetch(vi.fn().mockResolvedValue(statusResponse({ version_code: 3 })));

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(result.current.update).not.toBeNull());
    expect(result.current.update).toEqual({ code: 3, versionName: null });
  });

  it("dismiss() persists the dismissed code and clears the update", async () => {
    stubFetch(
      vi.fn().mockResolvedValue(
        statusResponse({ version_code: 5, version_name: "1.5.0" }),
      ),
    );

    const { result } = renderHook(() => useNativeAppUpdate());

    await waitFor(() => expect(result.current.update).not.toBeNull());
    act(() => result.current.dismiss());

    expect(result.current.update).toBeNull();
    expect(localStorage.getItem(DISMISS_KEY)).toBe("5");
  });

  it("dismiss() is a no-op when nothing is shown (does not stamp storage)", async () => {
    // Web platform: no update ever surfaces, but dismiss must stay safe to call.
    mocks.native = false;
    const { result } = renderHook(() => useNativeAppUpdate());

    act(() => result.current.dismiss());

    expect(result.current.update).toBeNull();
    expect(localStorage.getItem(DISMISS_KEY)).toBeNull();
  });

  it("re-checks when the app returns to active and ignores non-active", async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValue(statusResponse({ version_code: 5 }));
    stubFetch(fetchSpy);

    const { result } = renderHook(() => useNativeAppUpdate());
    await waitFor(() => expect(result.current.update).not.toBeNull());
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Backend stages an even newer build while backgrounded.
    fetchSpy.mockResolvedValue(statusResponse({ version_code: 9 }));

    // Non-active state changes must NOT trigger a re-check.
    const before = fetchSpy.mock.calls.length;
    act(() => activeListener!({ isActive: false }));
    expect(fetchSpy.mock.calls.length).toBe(before);

    // Returning active re-checks and picks up the newer build.
    act(() => activeListener!({ isActive: true }));
    await waitFor(() =>
      expect(result.current.update).toEqual({ code: 9, versionName: null }),
    );
  });

  it("removes the app-state listener on unmount", async () => {
    stubFetch(vi.fn().mockResolvedValue(statusResponse({ version_code: 5 })));

    const { unmount } = renderHook(() => useNativeAppUpdate());
    await waitFor(() => expect(mocks.app.addListener).toHaveBeenCalled());
    unmount();
    // The cleanup awaits the listener handle before calling remove().
    await act(async () => {
      await Promise.resolve();
    });

    expect(mocks.remove).toHaveBeenCalled();
  });
});
