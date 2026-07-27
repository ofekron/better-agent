// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const updater = vi.hoisted(() => ({
  notifyAppReady: vi.fn(),
  current: vi.fn(),
  download: vi.fn(),
  set: vi.fn(),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => true },
}));

vi.mock("@capgo/capacitor-updater", () => ({
  CapacitorUpdater: updater,
}));

vi.mock("../src/bearerAuth", () => ({
  getStoredToken: () => "token",
}));

vi.mock("../src/api", () => ({
  API: "https://backend.test",
}));

import {
  initializeMobileUpdater,
  runMobileOtaCheck,
} from "../src/lib/mobileUpdater";

function manifestResponse(version: string): Response {
  return {
    ok: true,
    json: async () => ({
      version,
      checksum: "checksum",
      download_path: "/api/mobile/bundle/download?ticket=ticket",
    }),
  } as Response;
}

describe("mobile updater", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    updater.notifyAppReady.mockReset().mockResolvedValue({});
    updater.current.mockReset();
    updater.download.mockReset();
    updater.set.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("waits for the running bundle to commit before starting the deferred network check", async () => {
    vi.useFakeTimers();
    let resolveCommit!: () => void;
    updater.notifyAppReady.mockImplementation(
      () => new Promise((resolve) => {
        resolveCommit = () => resolve({});
      }),
    );
    vi.mocked(fetch).mockResolvedValue(manifestResponse("current-version"));
    updater.current.mockResolvedValue({
      bundle: { version: "current-version" },
      native: "1.0.0",
    });

    initializeMobileUpdater(vi.fn());
    await vi.waitFor(() => expect(updater.notifyAppReady).toHaveBeenCalledOnce());

    expect(fetch).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3_000);
    expect(fetch).not.toHaveBeenCalled();

    resolveCommit();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  });

  it("does not check or activate updates when the running bundle cannot be committed", async () => {
    vi.useFakeTimers();
    const onCommitError = vi.fn();
    const error = new Error("commit failed");
    updater.notifyAppReady.mockRejectedValue(error);

    initializeMobileUpdater(onCommitError);
    await vi.advanceTimersByTimeAsync(3_000);

    expect(onCommitError).toHaveBeenCalledWith(error);
    expect(fetch).not.toHaveBeenCalled();
    expect(updater.current).not.toHaveBeenCalled();
    expect(updater.set).not.toHaveBeenCalled();
  });

  it("does not reactivate the bundle when the manifest version is already current", async () => {
    vi.mocked(fetch).mockResolvedValue(manifestResponse("current-version"));
    updater.current.mockResolvedValue({
      bundle: { version: "current-version" },
      native: "1.0.0",
    });

    await runMobileOtaCheck();

    expect(updater.download).not.toHaveBeenCalled();
    expect(updater.set).not.toHaveBeenCalled();
    expect(updater.notifyAppReady).not.toHaveBeenCalled();
  });

  it("downloads and activates a genuinely newer bundle once", async () => {
    vi.mocked(fetch).mockResolvedValue(manifestResponse("new-version"));
    updater.current.mockResolvedValue({
      bundle: { version: "old-version" },
      native: "1.0.0",
    });
    updater.download.mockResolvedValue({ id: "bundle-id" });

    await runMobileOtaCheck();

    expect(updater.download).toHaveBeenCalledWith({
      url: "https://backend.test/api/mobile/bundle/download?ticket=ticket",
      version: "new-version",
      checksum: "checksum",
    });
    expect(updater.set).toHaveBeenCalledWith({ id: "bundle-id" });
    expect(updater.notifyAppReady).not.toHaveBeenCalled();
  });
});
