import { afterEach, describe, expect, it, vi } from "vitest";
import { notifyUserRequest } from "../src/utils/userInputNotifications";
import type { UserInteractionRequest } from "../src/types";

const approval: UserInteractionRequest = {
  request_id: "approval-1",
  app_session_id: "session-1",
  kind: "approval",
  prompt: "Deploy the release?",
  status: "pending",
  created_at: 1,
};

const originalNotification = globalThis.Notification;

afterEach(() => {
  Object.defineProperty(window, "pywebview", { value: undefined, configurable: true });
  Object.defineProperty(globalThis, "Notification", {
    value: originalNotification,
    configurable: true,
  });
});

describe("user request notifications", () => {
  it("uses the desktop bridge when available", async () => {
    const notifyUser = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "pywebview", {
      value: { api: { notify_user: notifyUser } },
      configurable: true,
    });
    await notifyUserRequest(approval, "Approval needed", "Input needed");
    expect(notifyUser).toHaveBeenCalledWith("Approval needed", "Deploy the release?");
  });

  it("does not request browser permission from a background event", async () => {
    Object.defineProperty(window, "pywebview", { value: undefined, configurable: true });
    const requestPermission = vi.fn();
    Object.defineProperty(globalThis, "Notification", {
      value: Object.assign(vi.fn(), { permission: "default", requestPermission }),
      configurable: true,
    });
    await notifyUserRequest(approval, "Approval needed", "Input needed");
    expect(requestPermission).not.toHaveBeenCalled();
  });
});
