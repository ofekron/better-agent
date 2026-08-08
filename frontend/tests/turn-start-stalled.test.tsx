import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { RunBadge } from "../src/components/RunBadge";
import type { RunInfo } from "../src/types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const stalledRun: RunInfo = {
  run_id: "run-1",
  kind: "native",
  target_message_id: "assistant-1",
  pid: 123,
  started_at: "2026-01-01T00:00:00Z",
  last_event_at: "2026-01-01T00:00:00Z",
  provider_kind: "codex",
  startup_phase: "stalled",
  startup_expected_activity: "task_started",
  startup_silence_threshold_seconds: 90,
  stalled_at: "2026-01-01T00:01:30Z",
};

describe("turn startup stalled state", () => {
  it("shows truthful controls without invoking either action automatically", () => {
    // `RunBadge` also eagerly cold-hydrates the v2 `runs` feed on mount
    // (`useRunSummary` — see runSummaryRegistry.ts) as an ADDITIONAL
    // stalled-honesty signal layered on top of this legacy-stalled render;
    // that GET is unrelated to "does clicking nothing invoke an action",
    // which is what this test actually guards — only the STOP-triggering
    // call must stay silent without a user click.
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    render(<RunBadge run={stalledRun} sessionId="session-1" />);

    expect(screen.getByRole("status").textContent).toContain("Codex has not started the task");
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Retry becomes available/)).not.toBeNull();
    expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/stop"))).toBe(false);
  });

  it("cancels only after the user clicks Cancel", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    render(<RunBadge run={stalledRun} sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/api/sessions/session-1/stop"))).toBe(true),
    );
    expect((screen.getByRole("button", { name: "Cancelling…" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
