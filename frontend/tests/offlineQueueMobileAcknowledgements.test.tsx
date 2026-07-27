import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getAcknowledgements: vi.fn(),
  clearAcknowledgements: vi.fn(),
  addListener: vi.fn(),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => true },
}));
vi.mock("@capacitor/app", () => ({
  App: {
    addListener: mocks.addListener,
  },
}));
vi.mock("../src/lib/mobileBackgroundSync", () => ({
  getMobileBackgroundAcknowledgements: mocks.getAcknowledgements,
  clearMobileBackgroundAcknowledgements: mocks.clearAcknowledgements,
  scheduleMobileBackgroundSyncMirror: vi.fn(),
}));

import { useOfflineQueue } from "../src/hooks/useOfflineQueue";

const STORAGE_KEY = "better_agent_offline_queue";

describe("mobile offline acknowledgement reconciliation", () => {
  beforeEach(() => {
    mocks.getAcknowledgements.mockReset();
    mocks.clearAcknowledgements.mockReset().mockResolvedValue(undefined);
    mocks.addListener.mockReset().mockResolvedValue({ remove: vi.fn() });
  });

  it("durably removes matching create/send actions before clearing runner acknowledgements", async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([
      {
        type: "create_session",
        clientId: "create-1",
        session: { id: "11111111-1111-4111-8111-111111111111" },
        prompt: "create",
      },
      {
        type: "send_message",
        clientId: "send-1",
        sessionId: "22222222-2222-4222-8222-222222222222",
        prompt: "send",
        model: "model",
        cwd: "/tmp",
      },
    ]));
    mocks.getAcknowledgements.mockResolvedValue([
      { sessionId: "11111111-1111-4111-8111-111111111111", clientId: "create-1" },
      { sessionId: "22222222-2222-4222-8222-222222222222", clientId: "send-1" },
    ]);

    const { result } = renderHook(() => useOfflineQueue());

    await waitFor(() => expect(result.current.queue).toEqual([]));
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(mocks.clearAcknowledgements).toHaveBeenCalledOnce();
  });

  it("retains runner acknowledgements when durable queue persistence fails", async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([{
      type: "send_message",
      clientId: "send-1",
      sessionId: "22222222-2222-4222-8222-222222222222",
      prompt: "send",
      model: "model",
      cwd: "/tmp",
    }]));
    mocks.getAcknowledgements.mockResolvedValue([
      { sessionId: "22222222-2222-4222-8222-222222222222", clientId: "send-1" },
    ]);
    const removeItem = vi.spyOn(localStorage, "removeItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });

    const { result } = renderHook(() => useOfflineQueue());

    await waitFor(() => expect(result.current.persistFailed).toBe(true));
    expect(mocks.clearAcknowledgements).not.toHaveBeenCalled();
    removeItem.mockRestore();
  });
});
