// B1/B3 parity restoration: legacy showed distinguishable kind-labeled
// running badges (manager/worker/native); the native RunningIndicator
// collapsed to one generic pulse per turn/panel with no kind distinction.
// `RunningIndicator` itself is pure presentational (no data lookup of its
// own) — this covers the leaf's own rendering contract for its `kind`
// prop. The data-lookup half (`useRunSummaryByTurn`) is covered by
// tests/useRunSummary.test.tsx; TurnView's end-to-end wiring is covered by
// tests/surface/runKindLabel.test.tsx.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../../src/i18n";
import { RunningIndicator } from "../../src/surface/leaf/RunningIndicator";

describe("RunningIndicator kind label", () => {
  it("no kind: plain 'running' label, matching legacy's native (empty-label) case", () => {
    render(<RunningIndicator />);
    const badge = screen.getByTestId("surface-run-badge");
    expect(badge.querySelector(".run-badge-label")?.textContent).toBe("running");
    expect(badge.getAttribute("data-kind")).toBeNull();
  });

  it("kind=manager: shows the raw, un-i18n'd 'manager' label, matching legacy verbatim", () => {
    render(<RunningIndicator kind="manager" />);
    const badge = screen.getByTestId("surface-run-badge");
    expect(badge.querySelector(".run-badge-label")?.textContent).toBe("manager running");
    expect(badge.getAttribute("data-kind")).toBe("manager");
  });

  it("kind=worker: shows the raw 'worker' label", () => {
    render(<RunningIndicator kind="worker" />);
    expect(screen.getByTestId("surface-run-badge").querySelector(".run-badge-label")?.textContent).toBe(
      "worker running",
    );
  });

  it("kind=native: no label prefix, same as no kind at all (legacy's own empty-string case)", () => {
    render(<RunningIndicator kind="native" />);
    const badge = screen.getByTestId("surface-run-badge");
    expect(badge.querySelector(".run-badge-label")?.textContent).toBe("running");
    expect(badge.getAttribute("data-kind")).toBe("native");
  });
});
