// `lifecycle_notice` node — LifecycleNoticeView (src/surface/nodes/LifecycleNotice.tsx)
// dispatches by payload.kind to the matching pill leaf. Ported gap closure
// for tests/messagebubble-lifecyclenotice.test.tsx / tests/unknown-lifecycle-events.test.ts
// (deleted): the native dispatch component existed but had zero test coverage.
// The legacy "compacted prompt replay" (expand to view pre-compaction transcript)
// case is NOT ported — CompactionPayloadWire.replaced_node_ids is never read
// anywhere in src/ (grep-confirmed); that affordance has no native implementation
// yet, flagged as a product gap in the migration ledger, not fabricated here.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LifecycleNoticeView } from "../../src/surface/nodes/LifecycleNotice";
import { node, resetSeq } from "./fixtures";

resetSeq();

function lifecycleNode(kind: string, data: Record<string, unknown> = {}) {
  return node({
    node_id: "lc1",
    turn_id: "t1",
    kind: "lifecycle_notice",
    payload: { kind, data },
  });
}

describe("LifecycleNoticeView dispatch", () => {
  it("null payload renders nothing", () => {
    const n = node({ node_id: "lc0", turn_id: "t1", kind: "lifecycle_notice", payload: null });
    const { container } = render(<LifecycleNoticeView node={n} />);
    expect(container.firstChild).toBeNull();
  });

  it("retrying: renders the countdown pill keyed on retry_at", () => {
    const retryAt = new Date(Date.now() + 30_000).toISOString();
    render(<LifecycleNoticeView node={lifecycleNode("retrying", { retry_at: retryAt })} />);
    expect(screen.getByTestId("surface-retrying-pill")).toBeTruthy();
  });

  it("retrying: null retry_at renders nothing", () => {
    const { container } = render(<LifecycleNoticeView node={lifecycleNode("retrying", {})} />);
    expect(container.firstChild).toBeNull();
  });

  it("detached: renders the reconnecting pill", () => {
    render(<LifecycleNoticeView node={lifecycleNode("detached")} />);
    expect(screen.getByText(/Reconnecting/)).toBeTruthy();
  });

  it("recovering: renders the recovering pill", () => {
    render(<LifecycleNoticeView node={lifecycleNode("recovering")} />);
    expect(screen.getByTestId("surface-recovering-pill")).toBeTruthy();
  });

  it("auto_retried: singular vs pluralized count, rate_limit vs transient vs default copy", () => {
    const { rerender } = render(<LifecycleNoticeView node={lifecycleNode("auto_retried", { retry_kind: "rate_limit", count: 1 })} />);
    expect(screen.getByText(/Auto-retried after rate limit/)).toBeTruthy();
    expect(screen.queryByText(/×/)).toBeNull();

    rerender(<LifecycleNoticeView node={lifecycleNode("auto_retried", { retry_kind: "transient", count: 3 })} />);
    expect(screen.getByText(/Auto-retried after a transient error/)).toBeTruthy();
    expect(screen.getByText(/×3/)).toBeTruthy();

    rerender(<LifecycleNoticeView node={lifecycleNode("auto_retried", { count: 1 })} />);
    expect(screen.getByTestId("surface-auto-retry-pill").textContent).toContain("Auto-retried");
  });

  it("rate_limited: renders the retry-warning box with the given text", () => {
    render(<LifecycleNoticeView node={lifecycleNode("rate_limited", { text: "429 from provider" })} />);
    const box = screen.getByTestId("surface-retry-warning");
    expect(box.textContent).toContain("429 from provider");
  });

  it("unrecognized kind renders nothing", () => {
    const { container } = render(<LifecycleNoticeView node={lifecycleNode("some_future_kind")} />);
    expect(container.firstChild).toBeNull();
  });
});
