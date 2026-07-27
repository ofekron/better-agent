import { beforeEach, describe, expect, it, vi } from "vitest";

const { dispatchEvent, isNativePlatform } = vi.hoisted(() => ({
  dispatchEvent: vi.fn(),
  isNativePlatform: vi.fn(() => true),
}));

vi.mock("@capacitor/background-runner", () => ({
  BackgroundRunner: { dispatchEvent },
}));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform },
}));

import { mirrorMobileBackgroundSyncState } from "../src/lib/mobileBackgroundSync";

describe("mobile background sync state", () => {
  beforeEach(() => {
    dispatchEvent.mockReset();
    isNativePlatform.mockReturnValue(true);
    localStorage.clear();
  });

  it("mirrors the durable queue with the current server and access token", async () => {
    const actions = [{ sessionId: "session-1", clientId: "client-1", prompt: "hello" }];
    localStorage.setItem("better_agent_server_url", "https://agent.example/");
    localStorage.setItem("better_agent_auth_token", "access-token");
    localStorage.setItem("better_agent_offline_queue", JSON.stringify(actions));

    await mirrorMobileBackgroundSyncState();

    expect(dispatchEvent).toHaveBeenCalledWith({
      label: "com.betteragent.offline-sync",
      event: "updateOfflineState",
      details: {
        state: JSON.stringify({
          serverUrl: "https://agent.example",
          accessToken: "access-token",
          actions,
        }),
      },
    });
  });

  it("clears runner state when authentication is unavailable", async () => {
    localStorage.setItem("better_agent_server_url", "https://agent.example");

    await mirrorMobileBackgroundSyncState();

    expect(dispatchEvent).toHaveBeenCalledWith({
      label: "com.betteragent.offline-sync",
      event: "clearOfflineState",
      details: {},
    });
  });

  it("does not expose state outside native apps", async () => {
    isNativePlatform.mockReturnValue(false);

    await mirrorMobileBackgroundSyncState();

    expect(dispatchEvent).not.toHaveBeenCalled();
  });
});
