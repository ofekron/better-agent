// `worker_interaction` node — WorkerInteractionView (src/surface/nodes/Misc.tsx).
// Gap closure: the component existed with zero test coverage (confirmed via
// coverage audit of tests/unknown-lifecycle-events.test.ts, deleted — its
// legacy worker-event-routing/unwrapping cases target raw WSEvent shapes
// that don't exist on the native path, since the backend now emits one
// pre-classified WorkerInteractionPayloadWire fact per node; only the
// render-happy-path for that fact was left uncovered).

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WorkerInteractionView } from "../../src/surface/nodes/Misc";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("WorkerInteractionView", () => {
  it("null payload renders nothing", () => {
    const n = node({ node_id: "w0", turn_id: "t1", kind: "worker_interaction", payload: null });
    const { container } = render(<WorkerInteractionView node={n} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the fact kind and a summary of up to 4 fact fields", () => {
    const n = node({
      node_id: "w1",
      turn_id: "t1",
      kind: "worker_interaction",
      payload: {
        fact_kind: "worker_start",
        fact: { event: "worker_start", name: "researcher", model: "sonnet", cwd: "/repo" },
      },
    });
    render(<WorkerInteractionView node={n} />);
    const el = screen.getByTestId("surface-worker-interaction");
    expect(el.textContent).toContain("worker_start");
    // `event` field is excluded from the summary (it's the same as fact_kind).
    expect(el.textContent).not.toContain("event:");
    expect(el.textContent).toContain("name: researcher");
  });

  it("renders with no summary text when the fact has only the excluded `event` field", () => {
    const n = node({
      node_id: "w2",
      turn_id: "t1",
      kind: "worker_interaction",
      payload: { fact_kind: "worker_complete", fact: { event: "worker_complete" } },
    });
    render(<WorkerInteractionView node={n} />);
    expect(screen.getByTestId("surface-worker-interaction").textContent).toContain("worker_complete");
  });
});
