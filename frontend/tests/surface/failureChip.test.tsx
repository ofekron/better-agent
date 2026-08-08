// `failure` node — FailureView -> FailureChip (src/surface/nodes/Failure.tsx,
// src/surface/leaf/Chips.tsx). Ported gap closure: FailureChip existed with
// zero test coverage. NOT a port of tests/messagebubble-completeevent.test.tsx's
// failure-path assertions (deleted) — FailureChip has a structurally
// different shape (severity badge + label + retryable flag) than legacy's
// CompleteEvent error block ("Request failed" title + separate error-detail
// + rate-limited-until line); that specific title/detail/rate-limit
// formatting has no native equivalent and is flagged as a gap in the
// migration ledger, not fabricated here. The success-path token-usage
// summary (In/Out/Cache) has NO native renderer at all — TurnEntry.usage
// (src/surface/state.ts) is captured but never read by any surface
// component — also flagged as a gap, not testable until implemented.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FailureView } from "../../src/surface/nodes/Failure";
import { node, resetSeq } from "./fixtures";

resetSeq();

function failureNode(overrides: Partial<{ code: string; text: string; severity: string; retryable: boolean; resolution: string | null }>) {
  return node({
    node_id: "f1",
    turn_id: "t1",
    kind: "failure",
    payload: {
      code: "boom",
      text: "boom",
      data: null,
      severity: "error",
      retryable: false,
      resolution: null,
      ...overrides,
    },
  });
}

describe("FailureView / FailureChip", () => {
  it("null payload renders nothing", () => {
    const n = node({ node_id: "f0", turn_id: "t1", kind: "failure", payload: null });
    const { container } = render(<FailureView node={n} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders severity badge and label text", () => {
    render(<FailureView node={failureNode({ severity: "error", text: "boom" })} />);
    const chip = screen.getByTestId("surface-failure-chip");
    expect(chip.textContent).toContain("boom");
  });

  it("shows a Retryable marker only when retryable is true", () => {
    const { rerender } = render(<FailureView node={failureNode({ retryable: false })} />);
    expect(screen.getByTestId("surface-failure-chip").textContent).not.toContain("Retryable");

    rerender(<FailureView node={failureNode({ retryable: true })} />);
    expect(screen.getByTestId("surface-failure-chip").textContent).toContain("Retryable");
  });

  it("resolution: choose_fallback renders the rate-limit box instead of the plain chip", () => {
    render(<FailureView node={failureNode({ resolution: "choose_fallback", text: "rate limited" })} />);
    expect(screen.getByTestId("surface-retry-warning")).toBeTruthy();
    expect(screen.queryByTestId("surface-failure-chip")).toBeNull();
  });
});
