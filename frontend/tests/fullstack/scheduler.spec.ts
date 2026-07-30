import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

/** Formats a Date as the value of <input type="datetime-local">:
 * `YYYY-MM-DDTHH:mm` in the local zone (no offset), mirroring
 * ScheduleSendPopover's own `toLocalInputValue`. */
function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

// Validates the real scheduler extension wiring: the composer's overflow
// menu → ScheduleSendPopover → POST to the scheduler extension's
// backend-dispatch route creates a REAL persisted schedule, and the
// standalone Schedules page (GET /api/schedules) reflects it — no mocks.
test("schedules a prompt from the composer and sees it on the Schedules page", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Say hello. Keep it short.");

  const scheduledPrompt = `Scheduled fullstack test prompt ${Date.now()}`;
  const draft = page.getByTestId("input-textarea");
  await draft.fill(scheduledPrompt);

  await page.locator(".input-overflow-trigger").click();
  const scheduleBtn = page.getByTestId("schedule-btn");
  await expect(scheduleBtn).toBeEnabled();
  await scheduleBtn.click();

  const popover = page.getByTestId("schedule-popover");
  await expect(popover).toBeVisible();

  const fireAt = toLocalInputValue(new Date(Date.now() + 2 * 60 * 60 * 1000));
  await page.getByTestId("schedule-fire-at").fill(fireAt);
  await page.getByTestId("schedule-repeat").selectOption("once");

  await page.getByTestId("schedule-submit").click();
  await expect(popover).not.toBeVisible({ timeout: 10_000 });

  // Composer draft is cleared once the backend owns the schedule.
  await expect(draft).toHaveValue("");

  await page.goto(`${backend.baseURL}/schedules`);
  const row = page.locator(".schedules-row", { hasText: scheduledPrompt });
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row.locator(".schedules-kind")).toHaveText(/once/i);
});
