import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SchedulesPage } from "../src/components/SchedulesPage";
import { eventBus } from "../src/lib/eventBus";
import type { Schedule } from "../src/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeSchedule(over: Partial<Schedule> = {}): Schedule {
  return {
    id: "sch1",
    app_session_id: "sess-a",
    prompt: "run the nightly report",
    kind: "once",
    fire_at: "2026-07-03T10:00:00",
    interval_seconds: null,
    created_at: "2026-07-01T10:00:00",
    last_fired_at: null,
    session_name: "My Session",
    session_exists: true,
    ...over,
  };
}

function stubFetch(responses: { schedules: Schedule[] }[]) {
  const calls: { url: string; method: string }[] = [];
  let getCount = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url: String(url), method });
      if (method === "DELETE") {
        return new Response(JSON.stringify({ success: true }), { status: 200 });
      }
      const body = responses[Math.min(getCount, responses.length - 1)];
      getCount += 1;
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SchedulesPage", () => {
  it("renders schedules from all sessions with session shortcuts", async () => {
    stubFetch([
      {
        schedules: [
          makeSchedule(),
          makeSchedule({
            id: "sch2",
            app_session_id: "sess-b",
            prompt: "water the plants",
            kind: "recurring",
            interval_seconds: 3600,
            session_name: "Other Session",
          }),
          makeSchedule({
            id: "sch3",
            app_session_id: "gone",
            prompt: "orphan prompt",
            session_name: null,
            session_exists: false,
          }),
        ],
      },
    ]);
    const onOpenSession = vi.fn();
    render(<SchedulesPage onBack={() => {}} onOpenSession={onOpenSession} />);

    await waitFor(() => expect(screen.getByText("run the nightly report")).toBeTruthy());
    expect(screen.getByText("water the plants")).toBeTruthy();
    expect(screen.getByText("orphan prompt")).toBeTruthy();
    expect(screen.getByText("schedulesPage.orphanSession")).toBeTruthy();

    fireEvent.click(screen.getByText("Other Session"));
    expect(onOpenSession).toHaveBeenCalledWith("/s/sess-b");
  });

  it("cancels a schedule via DELETE /api/schedules/{id}", async () => {
    const calls = stubFetch([{ schedules: [makeSchedule()] }]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("run the nightly report")).toBeTruthy());

    fireEvent.click(screen.getByLabelText("schedules.cancelTitle"));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "DELETE" && c.url.endsWith("/api/schedules/sch1")),
      ).toBe(true),
    );
  });

  it("refetches when a cross-session schedules_changed ping arrives", async () => {
    stubFetch([
      { schedules: [makeSchedule()] },
      {
        schedules: [
          makeSchedule(),
          makeSchedule({
            id: "new-from-other-session",
            app_session_id: "sess-z",
            prompt: "created elsewhere",
            session_name: "Z",
          }),
        ],
      },
    ]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("run the nightly report")).toBeTruthy());
    expect(screen.queryByText("created elsewhere")).toBeNull();

    act(() => {
      eventBus.publish("schedules_changed", {});
    });
    await waitFor(() => expect(screen.getByText("created elsewhere")).toBeTruthy());
  });
});
