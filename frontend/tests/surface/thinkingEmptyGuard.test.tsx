// Regression coverage: an empty (non-redacted) `thinking` node used to
// still render `ThinkingBlock` (with an empty thought string), producing
// a visible-but-blank "thinking" chip in the chat panel. `AssistantTextView`
// right above `ThinkingView` in ContentLeaves.tsx already guarded on
// `!payload.text`; `ThinkingView` did not. Fixed by mirroring that guard,
// carving out the `redacted` case (which has no `text` but IS meant to
// render its own placeholder).
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ThinkingView } from "../../src/surface/nodes/ContentLeaves";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("ThinkingView empty-text guard", () => {
  it("renders nothing for an empty, non-redacted thinking payload", () => {
    const n = node({
      kind: "thinking",
      node_id: "think-empty",
      turn_id: "t1",
      payload: { text: "", redacted: false },
    });
    const { container } = render(<ThinkingView node={n} />);
    expect(container.innerHTML).toBe("");
  });

  it("still renders the redacted placeholder when text is empty but redacted is true", () => {
    const n = node({
      kind: "thinking",
      node_id: "think-redacted",
      turn_id: "t1",
      payload: { text: "", redacted: true },
    });
    const { container } = render(<ThinkingView node={n} />);
    expect(container.innerHTML).not.toBe("");
  });

  it("still renders real thinking content", async () => {
    const n = node({
      kind: "thinking",
      node_id: "think-real",
      turn_id: "t1",
      payload: { text: "planning the approach", redacted: false },
    });
    const { container } = render(<ThinkingView node={n} />);
    await waitFor(() => expect(container.innerHTML).not.toBe(""));
  });
});
