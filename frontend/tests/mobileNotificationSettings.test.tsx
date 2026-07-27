import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { MobileNotificationSettings } from "../src/components/MobileNotificationSettings";

vi.mock("../src/utils/mobilePushNotifications", () => ({
  getOrCreateMobilePushDeviceId: () => Promise.resolve("device-1"),
}));

describe("MobileNotificationSettings", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads backend preferences and confirms a category change from the response", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        notification_preferences: {
          pending_approvals: true,
          pending_questions: false,
          completed_turns: true,
        },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        notification_preferences: {
          pending_approvals: true,
          pending_questions: true,
          completed_turns: true,
        },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    render(<MobileNotificationSettings />);

    const checkboxes = await screen.findAllByRole("checkbox");
    expect((checkboxes[1] as HTMLInputElement).checked).toBe(false);

    fireEvent.click(checkboxes[1]);

    await waitFor(() => expect((checkboxes[1] as HTMLInputElement).checked).toBe(true));
    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("/api/push-tokens/device-1/notification-preferences"),
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ notification_preferences: { pending_questions: true } }),
      }),
    );
  });
});
