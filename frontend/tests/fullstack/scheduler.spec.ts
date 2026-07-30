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

// Validates SchedulesPage's cancel button (`.schedules-danger` in
// `.schedules-row-actions`, calling `deleteScheduleById` → DELETE
// `/api/schedules/{id}`) actually removes the schedule server-side, not
// just from the in-memory list — survives a real reload and is gone from
// GET /api/schedules.
test("deletes a schedule from the Schedules page and it stays gone server-side", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Say hello. Keep it short.");

  const scheduledPrompt = `Scheduled delete test prompt ${Date.now()}`;
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

  await page.goto(`${backend.baseURL}/schedules`);
  const row = page.locator(".schedules-row", { hasText: scheduledPrompt });
  await expect(row).toBeVisible({ timeout: 15_000 });

  // The row's own delete/cancel action, scoped to this row so it can't
  // collide with the page-level "clear all" button that shares the same
  // `.schedules-danger` class.
  await row.locator(".schedules-row-actions .schedules-danger").click();
  await expect(row).toHaveCount(0, { timeout: 10_000 });

  // A real full reload must not resurrect it from stale client state.
  await page.reload();
  await expect(
    page.locator(".schedules-row", { hasText: scheduledPrompt }),
  ).toHaveCount(0, { timeout: 15_000 });

  // The backend itself must no longer list it.
  const res = await page.request.get(`${backend.baseURL}/api/schedules`);
  expect(res.ok()).toBe(true);
  const { schedules } = (await res.json()) as { schedules: Array<{ prompt: string }> };
  expect(schedules.some((s) => s.prompt === scheduledPrompt)).toBe(false);
});

// Validates the "daily" option in ScheduleSendPopover's `schedule-repeat`
// select (RECURRING.daily → kind: "recurring", interval_seconds: 86400)
// round-trips through the real backend: the Schedules page must render it
// as "Recurring" (`.schedules-kind`), not "Once".
test("schedules a recurring (daily) prompt and it persists as recurring on the Schedules page", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Say hello. Keep it short.");

  const scheduledPrompt = `Scheduled recurring test prompt ${Date.now()}`;
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
  await page.getByTestId("schedule-repeat").selectOption("daily");

  await page.getByTestId("schedule-submit").click();
  await expect(popover).not.toBeVisible({ timeout: 10_000 });

  await expect(draft).toHaveValue("");

  await page.goto(`${backend.baseURL}/schedules`);
  const row = page.locator(".schedules-row", { hasText: scheduledPrompt });
  await expect(row).toBeVisible({ timeout: 15_000 });
  // "Recurring" is schedulesPage.kindRecurring in en.json — distinct from
  // the "Once" label the other two tests assert on.
  await expect(row.locator(".schedules-kind")).toHaveText(/recurring/i);

  // Confirms the interval itself (86400s = "1d") was stored, not just the
  // kind discriminator.
  const res = await page.request.get(`${backend.baseURL}/api/schedules`);
  expect(res.ok()).toBe(true);
  const { schedules } = (await res.json()) as {
    schedules: Array<{ prompt: string; kind: string; interval_seconds: number | null }>;
  };
  const created = schedules.find((s) => s.prompt === scheduledPrompt);
  expect(created?.kind).toBe("recurring");
  expect(created?.interval_seconds).toBe(86400);
});
