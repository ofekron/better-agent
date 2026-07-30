// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const updater = vi.hoisted(() => ({
  notifyAppReady: vi.fn(),
  current: vi.fn(),
  getNextBundle: vi.fn(),
  getFailedUpdate: vi.fn(),
  list: vi.fn(),
  download: vi.fn(),
  next: vi.fn(),
}));

const auth = vi.hoisted(() => ({
  token: "token" as string | null,
}));

const platform = vi.hoisted(() => ({
  native: true,
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => platform.native },
}));

vi.mock("@capgo/capacitor-updater", () => ({
  CapacitorUpdater: updater,
}));

vi.mock("../src/bearerAuth", () => ({
  getStoredToken: () => auth.token,
}));

vi.mock("../src/api", () => ({
  API: "https://backend.test",
}));

import {
  initializeMobileUpdater,
  runMobileOtaCheck,
} from "../src/lib/mobileUpdater";

const STORAGE_KEY = "better_agent_mobile_ota_state";

function bundle(id: string, version: string, status = "success") {
  return { id, version, status, downloaded: "", checksum: "" };
}

function state(
  attempt: Record<string, unknown> | null = null,
  quarantined: Record<string, unknown> | null = null,
) {
  return { schema: 1, attempt, quarantined };
}

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

function setStoredState(value: unknown): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
}

function getStoredState() {
  return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
}

async function runReady(initialState: unknown = state(), readsFailure = true) {
  setStoredState(initialState);
  const ready = initializeMobileUpdater(vi.fn());
  ready();
  await vi.waitFor(() => expect(updater.notifyAppReady).toHaveBeenCalledOnce());
  await vi.waitFor(() => expect(updater.list).toHaveBeenCalled());
  if (readsFailure) {
    await vi.waitFor(() => expect(updater.getFailedUpdate).toHaveBeenCalled());
  }
  await Promise.resolve();
  return ready;
}

describe("mobile updater", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    auth.token = "token";
    platform.native = true;
    vi.stubGlobal("fetch", vi.fn());
    updater.notifyAppReady.mockReset().mockResolvedValue({
      bundle: bundle("current-id", "current-version"),
    });
    updater.current.mockReset().mockResolvedValue({
      bundle: bundle("current-id", "current-version"),
      native: "1.0.0",
    });
    updater.getNextBundle.mockReset().mockResolvedValue(null);
    updater.getFailedUpdate.mockReset().mockResolvedValue(null);
    updater.list.mockReset().mockResolvedValue({
      bundles: [bundle("current-id", "current-version")],
    });
    updater.download.mockReset();
    updater.next.mockReset().mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("starts no native work until the committed shell signals readiness", async () => {
    initializeMobileUpdater(vi.fn());
    await vi.advanceTimersByTimeAsync(20_000);

    expect(updater.notifyAppReady).not.toHaveBeenCalled();
    expect(updater.current).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("is a complete no-op on web", async () => {
    platform.native = false;
    const ready = initializeMobileUpdater(vi.fn());

    ready();
    await runMobileOtaCheck();
    await vi.advanceTimersByTimeAsync(20_000);

    expect(updater.notifyAppReady).not.toHaveBeenCalled();
    expect(updater.current).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("deduplicates concurrent shell-ready signals and defers the check until commit", async () => {
    vi.mocked(fetch).mockResolvedValue(manifestResponse("current-version"));
    const ready = initializeMobileUpdater(vi.fn());

    ready();
    ready();
    await vi.waitFor(() => expect(updater.notifyAppReady).toHaveBeenCalledOnce());
    expect(fetch).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3_000);
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("blocks checks when committing the running bundle fails", async () => {
    const onCommitError = vi.fn();
    const error = new Error("commit failed");
    updater.notifyAppReady.mockRejectedValue(error);
    const ready = initializeMobileUpdater(onCommitError);

    ready();
    await vi.waitFor(() => expect(onCommitError).toHaveBeenCalledWith(error));
    await vi.advanceTimersByTimeAsync(3_000);

    expect(fetch).not.toHaveBeenCalled();
  });

  it("commits the healthy current bundle but disables this boot on malformed state", async () => {
    setStoredState({ schema: 1, attempt: "invalid", quarantined: null });
    const ready = initializeMobileUpdater(vi.fn());

    ready();
    await vi.waitFor(() => expect(updater.notifyAppReady).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(3_000);

    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(updater.current).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps a matching pending activation and blocks another manifest check", async () => {
    const attempt = {
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduled",
    };
    updater.getNextBundle.mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );

    await runReady(state(attempt));
    await vi.advanceTimersByTimeAsync(3_000);

    expect(getStoredState().attempt).toEqual(attempt);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("promotes an interrupted scheduling phase when native next persisted", async () => {
    updater.getNextBundle.mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );

    await runReady(state({
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduling",
    }));

    expect(getStoredState().attempt.phase).toBe("scheduled");
  });

  it("clears an interrupted scheduling phase without quarantining when native next did not persist", async () => {
    await runReady(state({
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduling",
    }));

    expect(getStoredState()).toEqual(state());
  });

  it("clears a successfully activated target after committing it", async () => {
    const target = bundle("new-id", "new-version");
    updater.notifyAppReady.mockResolvedValue({ bundle: target });
    updater.current.mockResolvedValue({ bundle: target, native: "1.0.0" });
    updater.list.mockResolvedValue({ bundles: [target] });

    await runReady(state({
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "old-id",
      phase: "scheduled",
    }, {
      version: "new-version",
      id: "new-id",
    }), false);

    expect(getStoredState()).toEqual(state());
  });

  it("quarantines a scheduled target that reverted to the fallback", async () => {
    const attempt = {
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduled",
    };

    await runReady(state(attempt));

    expect(getStoredState()).toEqual(state(null, {
      version: "new-version",
      id: "new-id",
    }));
  });

  it("quarantines only exact native failure evidence", async () => {
    const attempt = {
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduling",
    };
    updater.getFailedUpdate.mockResolvedValue({
      bundle: bundle("unrelated-id", "old-version", "error"),
    });

    await runReady(state(attempt));
    expect(getStoredState()).toEqual(state());

    updater.notifyAppReady.mockClear();
    updater.current.mockClear();
    setStoredState(state(attempt));
    updater.getFailedUpdate.mockResolvedValue({
      bundle: bundle("new-id", "new-version", "error"),
    });
    await runReady(state(attempt));

    expect(getStoredState().quarantined).toEqual({
      version: "new-version",
      id: "new-id",
    });
  });

  it("does not consume destructive failure evidence when a native snapshot fails", async () => {
    const onStartupError = vi.fn();
    updater.list.mockRejectedValue(new Error("list failed"));
    setStoredState(state({
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduled",
    }));
    const ready = initializeMobileUpdater(onStartupError);

    ready();
    await vi.waitFor(() => expect(onStartupError).toHaveBeenCalledOnce());

    expect(updater.getFailedUpdate).not.toHaveBeenCalled();
    expect(getStoredState().attempt.id).toBe("new-id");
  });

  it("skips a quarantined manifest version", async () => {
    setStoredState(state(null, {
      version: "new-version",
      id: "new-id",
    }));
    vi.mocked(fetch).mockResolvedValue(manifestResponse("new-version"));

    await runMobileOtaCheck();

    expect(updater.download).not.toHaveBeenCalled();
    expect(updater.next).not.toHaveBeenCalled();
  });

  it("does not check without a token or reactivate the current version", async () => {
    auth.token = null;
    await runMobileOtaCheck();
    expect(fetch).not.toHaveBeenCalled();

    auth.token = "token";
    vi.mocked(fetch).mockResolvedValue(manifestResponse("current-version"));
    await runMobileOtaCheck();

    expect(updater.download).not.toHaveBeenCalled();
    expect(updater.next).not.toHaveBeenCalled();
  });

  it("journals and schedules a genuinely newer bundle without reloading", async () => {
    vi.mocked(fetch).mockResolvedValue(manifestResponse("new-version"));
    updater.download.mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );

    await runMobileOtaCheck();

    expect(updater.download).toHaveBeenCalledWith({
      url: "https://backend.test/api/mobile/bundle/download?ticket=ticket",
      version: "new-version",
      checksum: "checksum",
    });
    expect(updater.next).toHaveBeenCalledWith({ id: "new-id" });
    expect(getStoredState().attempt).toEqual({
      version: "new-version",
      id: "new-id",
      sourceCurrentId: "current-id",
      phase: "scheduled",
    });
  });

  it("reconciles a scheduling rejection against native next state", async () => {
    vi.mocked(fetch).mockResolvedValue(manifestResponse("new-version"));
    updater.download.mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );
    updater.next.mockRejectedValue(new Error("uncertain scheduling result"));
    updater.getNextBundle.mockResolvedValue(
      bundle("new-id", "new-version", "pending"),
    );

    await runMobileOtaCheck();
    expect(getStoredState().attempt.phase).toBe("scheduled");

    setStoredState(state());
    updater.getNextBundle.mockResolvedValue(null);
    await runMobileOtaCheck();
    expect(getStoredState()).toEqual(state());
  });
});
