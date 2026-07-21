import React from "react";
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
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<RunBadge run={stalledRun} sessionId="session-1" />);

    expect(screen.getByRole("status").textContent).toContain("Codex has not started the task");
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Retry becomes available/)).not.toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("cancels only after the user clicks Cancel", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200 }),
    );
    render(<RunBadge run={stalledRun} sessionId="session-1" />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    expect(fetchSpy.mock.calls[0]?.[0]).toContain("/api/sessions/session-1/stop");
    expect((screen.getByRole("button", { name: "Cancelling…" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
