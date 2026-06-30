import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

const runsPayload = {
  runs: [
    {
      run_id: "run-1",
      mode: "native",
      started_at: "2026-06-30T10:00:00Z",
      target_message_id: null,
      prompt: "Babysit the dev server",
    },
  ],
};
const schedulesPayload = {
  schedules: [
    {
      id: "sched-1",
      app_session_id: "sess-1",
      prompt: "Check CI every hour",
      kind: "recurring",
      fire_at: "2026-06-30T11:00:00Z",
      interval_seconds: 3600,
      created_at: "2026-06-29T08:00:00Z",
      last_fired_at: null,
    },
  ],
};

vi.mock("../src/api", () => ({
  fetchSessionBackground: vi
    .fn()
    .mockResolvedValue(runsPayload),
  fetchSessionSchedules: vi
    .fn()
    .mockResolvedValue(schedulesPayload),
  killSessionBackground: vi.fn().mockResolvedValue({
    success: true,
    killed_run_ids: [],
  }),
  cancelSchedule: vi.fn().mockResolvedValue({ success: true }),
}));

const { SessionBackgroundStrip } = await import(
  "../src/components/SessionBackgroundStrip"
);

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SessionBackgroundStrip info expand", () => {
  it("unfolds run + schedule details on (i) click and hides them again", async () => {
    render(<SessionBackgroundStrip sessionId="sess-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("background-work-bar")).toBeTruthy(),
    );
    // Details hidden before expanding.
    expect(screen.queryByTestId("session-bg-details")).toBeNull();

    fireEvent.click(screen.getByTestId("background-info-btn"));

    const details = await screen.findByTestId("session-bg-details");
    // Run detail surfaces the originating prompt + mode.
    expect(details.textContent).toContain("Babysit the dev server");
    expect(details.textContent).toContain("native");
    // Schedule detail surfaces created/last-fired/interval.
    expect(details.textContent).toContain("Check CI every hour");
    expect(details.textContent).toContain("1h");
    expect(details.textContent).toContain("never");

    // Collapse on second click.
    fireEvent.click(screen.getByTestId("background-info-btn"));
    expect(screen.queryByTestId("session-bg-details")).toBeNull();
  });
});
