// B1/B3 parity restoration end-to-end: TurnView.tsx's own top-level live
// turn resolves a run's `kind` (manager/worker/native) via
// `useRunSummaryByTurn` (correlated by `turn_id`, since content nodes
// never carry a populated `run_ref`) and passes it to RunningIndicator.
// `runSummaryRegistry` is a true module-level singleton — reimported
// fresh per test (`vi.resetModules()`), same isolation pattern
// tests/useRunSummary.test.tsx already uses for the same singleton.

import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../src/i18n";
import { MockWebSocketController } from "../harness/mockWebSocket";
import type { RunSummaryWire } from "../../src/adapter/wire";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import { promptNode, resetSeq, turnNode } from "./fixtures";

resetSeq();

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as Response);
}

function runsEnvelope(runs: RunSummaryWire[]) {
  return {
    kind: "ok",
    runs,
    next_cursor: null,
    snapshot_identity: { incarnation: "i1", render_rev: 1, hist_rev: 1 },
  };
}

function runSummary(overrides: Partial<RunSummaryWire> = {}): RunSummaryWire {
  return {
    run_id: "run-1",
    session_id: "s1",
    turn_id: "t1",
    provider_id: "claude",
    runner: "native",
    phase: "running",
    started_at: 1_700_000_000,
    last_heartbeat_at: 1_700_000_005,
    startup: null,
    kind: null,
    ...overrides,
  };
}

class FakeSurfaceStore {
  getChildren(): undefined {
    return undefined;
  }
  async ensureChildren() {
    return [];
  }
  subscribe(): () => void {
    return () => {};
  }
}

function asStore(fake: FakeSurfaceStore): SurfaceStore {
  return fake as unknown as SurfaceStore;
}

const NO_RUNS = new Map();
const EMPTY = { renderable_child_count: 0, has_children: false };

function liveEntry(): TurnEntry {
  return {
    turnId: "t1",
    turn: turnNode("t1", EMPTY, "s1"),
    prompt: promptNode("t1", "hi", "s1"),
    results: [],
    manifest: EMPTY,
    runtimeChange: null,
    phase: "running",
    reason: null,
    usage: null,
    provisionalSend: null,
  };
}

let ws: MockWebSocketController;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  ws = new MockWebSocketController();
  ws.install();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  ws.uninstall();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("TurnView — live top-level turn shows the run's kind label", () => {
  it("resolves the manager kind via useRunSummaryByTurn and renders it on the badge", async () => {
    vi.resetModules();
    const { TurnView } = await import("../../src/surface/TurnView");
    fetchMock.mockResolvedValue(
      jsonResponse(runsEnvelope([runSummary({ turn_id: "t1", kind: "manager" })])),
    );

    render(<TurnView entry={liveEntry()} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);

    await waitFor(() =>
      expect(screen.getByTestId("surface-run-badge").querySelector(".run-badge-label")?.textContent).toBe(
        "manager running",
      ),
    );
  });

  it("no known run for this turn yet: plain generic badge, no crash", async () => {
    vi.resetModules();
    const { TurnView } = await import("../../src/surface/TurnView");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([])));

    render(<TurnView entry={liveEntry()} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.getByTestId("surface-run-badge").querySelector(".run-badge-label")?.textContent).toBe(
      "running",
    );
  });

  it("a live run_summary_upsert updates the kind label without remounting", async () => {
    vi.resetModules();
    const { TurnView } = await import("../../src/surface/TurnView");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([])));

    render(<TurnView entry={liveEntry()} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);
    await waitFor(() =>
      expect(screen.getByTestId("surface-run-badge").querySelector(".run-badge-label")?.textContent).toBe(
        "running",
      ),
    );

    act(() => {
      ws.emit({
        type: "run_summary_upsert",
        cv: 1,
        summary: runSummary({ turn_id: "t1", kind: "worker" }),
      } as never);
    });

    await waitFor(() =>
      expect(screen.getByTestId("surface-run-badge").querySelector(".run-badge-label")?.textContent).toBe(
        "worker running",
      ),
    );
  });
});
