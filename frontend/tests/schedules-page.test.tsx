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

  it("formats recurring intervals across day, minute, and sub-minute units", async () => {
    stubFetch([
      {
        schedules: [
          makeSchedule({ id: "d", kind: "recurring", interval_seconds: 86400, prompt: "daily job" }),
          makeSchedule({ id: "m", kind: "recurring", interval_seconds: 120, prompt: "minute job" }),
          makeSchedule({ id: "s", kind: "recurring", interval_seconds: 90, prompt: "second job" }),
        ],
      },
    ]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("daily job")).toBeTruthy());
    expect(screen.getByText("schedules.interval 1d")).toBeTruthy();
    expect(screen.getByText("schedules.interval 2m")).toBeTruthy();
    expect(screen.getByText("schedules.interval 90s")).toBeTruthy();
  });

  it("renders the empty state when there are no schedules", async () => {
    stubFetch([{ schedules: [] }]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("schedulesPage.empty")).toBeTruthy());
  });

  it("reloads the snapshot when the refresh button is clicked", async () => {
    const calls = stubFetch([
      { schedules: [makeSchedule({ prompt: "first load" })] },
      { schedules: [makeSchedule({ prompt: "after refresh" })] },
    ]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("first load")).toBeTruthy());
    fireEvent.click(screen.getByLabelText("schedulesPage.refresh"));
    await waitFor(() => expect(screen.getByText("after refresh")).toBeTruthy());
    expect(calls.filter((c) => c.method === "GET")).toHaveLength(2);
  });

  it("renders last-fired timestamps, invalid fire dates, and empty session names", async () => {
    stubFetch([
      {
        schedules: [
          makeSchedule({ id: "fired", prompt: "fired job", last_fired_at: "2026-07-02T09:00:00" }),
          makeSchedule({ id: "never", prompt: "never job", last_fired_at: null }),
          makeSchedule({ id: "baddate", prompt: "bad date job", fire_at: "not-a-date" }),
          makeSchedule({
            id: "noname",
            prompt: "no name job",
            session_exists: true,
            session_name: "",
          }),
        ],
      },
    ]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("fired job")).toBeTruthy());
    // last_fired_at present on one row → the other three show neverFired
    expect(screen.getAllByText("schedules.lastFired: schedules.neverFired")).toHaveLength(3);
    // invalid fire_at falls through to the raw value
    expect(screen.getByText("schedulesPage.nextFire: not-a-date")).toBeTruthy();
    // empty session_name with an existing session falls back to the session id
    expect(screen.getByText("sess-a")).toBeTruthy();
  });

  it("shows the error banner when the fetch rejects with an Error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("boom");
    }));
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("stringifies a non-Error fetch rejection into the error banner", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw "network down";
    }));
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("network down")).toBeTruthy());
  });

  it("removes the row after the cancel animation completes", async () => {
    stubFetch([{ schedules: [makeSchedule({ prompt: "going away" })] }]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("going away")).toBeTruthy());
    fireEvent.click(screen.getByLabelText("schedules.cancelTitle"));
    await waitFor(() => expect(screen.queryByText("going away")).toBeNull());
  });

  it("shows an error when cancelling a schedule fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if ((init?.method ?? "GET") === "DELETE") {
          return new Response("failmsg", { status: 500 });
        }
        return new Response(
          JSON.stringify({ schedules: [makeSchedule({ prompt: "stuck" })] }),
          { status: 200 },
        );
      }),
    );
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("stuck")).toBeTruthy());
    fireEvent.click(screen.getByLabelText("schedules.cancelTitle"));
    await waitFor(() => expect(screen.getByText("HTTP 500: failmsg")).toBeTruthy());
  });

  it("clears all schedules after confirmation and animates them out", async () => {
    const calls = stubFetch([
      {
        schedules: [
          makeSchedule({ id: "c1", prompt: "clear me one" }),
          makeSchedule({ id: "c2", app_session_id: "sess-b", prompt: "clear me two" }),
        ],
      },
    ]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("clear me one")).toBeTruthy());
    fireEvent.click(screen.getByText("schedulesPage.clearAll"));
    fireEvent.click(screen.getByText("schedulesPage.clearAllConfirm"));
    await waitFor(() => expect(screen.queryByText("clear me one")).toBeNull());
    expect(screen.queryByText("clear me two")).toBeNull();
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(2);
  });

  it("aborts clear-all without deleting when the confirm cancel button is pressed", async () => {
    const calls = stubFetch([{ schedules: [makeSchedule({ prompt: "keep me" })] }]);
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("keep me")).toBeTruthy());
    fireEvent.click(screen.getByText("schedulesPage.clearAll"));
    fireEvent.click(screen.getByText("app.cancel"));
    expect(screen.queryByText("schedulesPage.clearAllConfirm")).toBeNull();
    expect(screen.getByText("schedulesPage.clearAll")).toBeTruthy();
    expect(screen.getByText("keep me")).toBeTruthy();
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  });

  it("reports a clear-all failure and leaves the failed rows in place", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if ((init?.method ?? "GET") === "DELETE") {
          return url.endsWith("/api/schedules/c2")
            ? new Response("nope", { status: 500 })
            : new Response(JSON.stringify({ success: true }), { status: 200 });
        }
        return new Response(
          JSON.stringify({
            schedules: [
              makeSchedule({ id: "c1", prompt: "ok row" }),
              makeSchedule({ id: "c2", prompt: "bad row" }),
            ],
          }),
          { status: 200 },
        );
      }),
    );
    render(<SchedulesPage onBack={() => {}} onOpenSession={() => {}} />);
    await waitFor(() => expect(screen.getByText("ok row")).toBeTruthy());
    fireEvent.click(screen.getByText("schedulesPage.clearAll"));
    fireEvent.click(screen.getByText("schedulesPage.clearAllConfirm"));
    await waitFor(() => expect(screen.getByText("schedulesPage.clearAllFailed")).toBeTruthy());
    await waitFor(() => expect(screen.queryByText("ok row")).toBeNull());
    expect(screen.getByText("bad row")).toBeTruthy();
  });
});
