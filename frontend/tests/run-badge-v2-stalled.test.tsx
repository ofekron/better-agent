// ADR 0009 / Package E honesty-rule coverage: RunBadge's stalled render is
// the UNION of two independent signals — legacy `run.startup_phase ===
// "stalled"` (turn_manager heuristic, unchanged) and v2's heartbeat-derived
// `phase === "stalled"` (runs_adapter.py, disk-backed). `useRunSummary` is
// mocked directly here (its own coverage lives in useRunSummary.test.tsx) so
// this suite only exercises RunBadge's OWN branching between the three
// states: neither signal, legacy-only, v2-only.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { RunBadge } from "../src/components/RunBadge";
import type { RunInfo } from "../src/types";
import type { RunSummaryWire } from "../src/adapter/wire";

const mockUseRunSummary = vi.fn<(sessionId?: string, runId?: string) => RunSummaryWire | undefined>();
vi.mock("../src/hooks/useRunSummary", () => ({
  useRunSummary: (sessionId?: string, runId?: string) => mockUseRunSummary(sessionId, runId),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  mockUseRunSummary.mockReset();
});

const runningRun: RunInfo = {
  run_id: "run-1",
  kind: "native",
  target_message_id: "assistant-1",
  pid: 123,
  started_at: "2026-01-01T00:00:00Z",
  last_event_at: "2026-01-01T00:00:00Z",
};

const legacyStalledRun: RunInfo = {
  ...runningRun,
  provider_kind: "codex",
  startup_phase: "stalled",
  startup_silence_threshold_seconds: 90,
};

function v2Summary(overrides: Partial<RunSummaryWire> = {}): RunSummaryWire {
  return {
    run_id: "run-1",
    session_id: "session-1",
    turn_id: "t1",
    provider_id: "codex",
    runner: "native",
    phase: "running",
    started_at: 1_700_000_000,
    last_heartbeat_at: 1_700_000_000,
    startup: null,
    ...overrides,
  };
}

describe("RunBadge — stalled-honesty union (legacy vs v2)", () => {
  it("neither signal stalled -> normal running badge, no stalled card", () => {
    mockUseRunSummary.mockReturnValue(v2Summary({ phase: "running" }));
    render(<RunBadge run={runningRun} sessionId="session-1" />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button")).not.toBeNull();
  });

  it("legacy-only stalled -> the rich legacy stalled card (unchanged), not the minimal v2 variant", () => {
    mockUseRunSummary.mockReturnValue(v2Summary({ phase: "running" }));
    render(<RunBadge run={legacyStalledRun} sessionId="session-1" />);
    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Codex has not started the task");
    // Rich-card-only affordance: the disabled Retry button.
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeNull();
  });

  it("v2-only stalled -> the minimal stalled card, distinguishable from the rich legacy one", () => {
    mockUseRunSummary.mockReturnValue(
      v2Summary({ phase: "stalled", last_heartbeat_at: 1_700_000_000 }),
    );
    render(<RunBadge run={runningRun} sessionId="session-1" />);
    const status = screen.getByRole("status");
    expect(status.className).toContain("turn-stalled-card");
    // Never fabricates the legacy copy's provider-name title or a Retry
    // affordance it has no honest threshold to justify.
    expect(status.textContent).not.toContain("has not started the task");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel" })).not.toBeNull();
  });

  it("v2 undefined (not yet hydrated) never renders a stalled card on its own", () => {
    mockUseRunSummary.mockReturnValue(undefined);
    render(<RunBadge run={runningRun} sessionId="session-1" />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("both signals stalled -> the rich legacy card wins (legacyStalled takes priority)", () => {
    mockUseRunSummary.mockReturnValue(v2Summary({ phase: "stalled" }));
    render(<RunBadge run={legacyStalledRun} sessionId="session-1" />);
    expect(screen.getByRole("status").textContent).toContain("Codex has not started the task");
  });

  it("without a sessionId, useRunSummary is still called (undefined session) and never crashes", () => {
    mockUseRunSummary.mockReturnValue(undefined);
    render(<RunBadge run={runningRun} />);
    expect(mockUseRunSummary).toHaveBeenCalledWith(undefined, "run-1");
    // No sessionId -> plain non-clickable badge, no modal wiring.
    expect(screen.queryByRole("button")).toBeNull();
  });
});
