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

// Documents the REAL (lack of) past-`fire_at` validation: ScheduleSendPopover's
// `canSubmit` (frontend/src/components/ScheduleSendPopover.tsx) only checks
// the prompt and that a value is present — no min-date/past-time guard — and
// schedule_store.create() (backend/stores/schedule_store.py) only bounds
// `fire_at` from ABOVE (MAX_HORIZON), never from below. So a past `fire_at`
// is accepted end-to-end rather than blocked, and because it's already
// overdue the moment it's created, schedule_ticker (10s tick, backend/
// scheduler.py) treats it as due on its very next tick and fires it through
// the normal turn path — deleting the "once" record afterward — instead of
// sitting un-fired or being rejected up front.
test("schedules a prompt with a past fire_at is accepted (no validation) and fires promptly as overdue", async ({
  authedPage: page,
  backend,
}) => {
  await createSessionWithPrompt(page, "Say hello. Keep it short.");

  const scheduledPrompt = `Scheduled past fire_at test prompt ${Date.now()}`;
  const draft = page.getByTestId("input-textarea");
  await draft.fill(scheduledPrompt);

  await page.locator(".input-overflow-trigger").click();
  const scheduleBtn = page.getByTestId("schedule-btn");
  await expect(scheduleBtn).toBeEnabled();
  await scheduleBtn.click();

  const popover = page.getByTestId("schedule-popover");
  await expect(popover).toBeVisible();

  // A real past datetime — yesterday, well outside any clock-skew margin.
  const pastFireAt = toLocalInputValue(new Date(Date.now() - 24 * 60 * 60 * 1000));
  await page.getByTestId("schedule-fire-at").fill(pastFireAt);
  await page.getByTestId("schedule-repeat").selectOption("once");

  // No client-side past-time guard: submit stays enabled for a past value.
  const submitBtn = page.getByTestId("schedule-submit");
  await expect(submitBtn).toBeEnabled();
  await submitBtn.click();

  // No server-side rejection either: the popover closes like any other
  // successful schedule creation, not a validation error.
  await expect(popover).not.toBeVisible({ timeout: 10_000 });
  await expect(draft).toHaveValue("");

  // The schedule was accepted as already-overdue and fires on the ticker's
  // next pass, then (being "once") is deleted from the store. Poll instead
  // of asserting a fixed visible-then-gone order, since it can fire before
  // this test's first fetch lands.
  await expect(async () => {
    const res = await page.request.get(`${backend.baseURL}/api/schedules`);
    expect(res.ok()).toBe(true);
    const { schedules } = (await res.json()) as { schedules: Array<{ prompt: string }> };
    expect(schedules.some((s) => s.prompt === scheduledPrompt)).toBe(false);
  }).toPass({ timeout: 60_000 });
});

// Validates SchedulesPage's zero-schedules render (frontend/src/components/
// SchedulesPage.tsx): once the initial `fetchAllSchedules()` resolves with
// an empty list, it must show the real `.schedules-empty` copy
// (schedulesPage.empty → "No schedules" in en.json) instead of an empty
// `.schedules-list` or a blank/crashed page — no schedule is ever created
// in this test, so this is a genuinely fresh backend.
test("renders the empty state on a fresh backend with zero schedules", async ({
  authedPage: page,
  backend,
}) => {
  await page.goto(`${backend.baseURL}/schedules`);

  await expect(page.locator(".schedules-row")).toHaveCount(0);

  const empty = page.locator(".schedules-empty");
  await expect(empty).toBeVisible({ timeout: 15_000 });
  await expect(empty).toHaveText("No schedules");

  // The header and its controls still render sanely around the empty body.
  await expect(page.getByRole("heading", { name: "Schedules" })).toBeVisible();
  // "Clear all" only renders when there's something to clear.
  await expect(page.getByRole("button", { name: "Clear all" })).toHaveCount(0);
});
