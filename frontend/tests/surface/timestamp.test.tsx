// A3 restoration: legacy `MessageBubble.tsx`'s per-message timestamp
// footer (`fmtTime`, `.message-box-footer`/`.user-message-time`) had no
// native equivalent — `NodeWire.ts` existed on the wire but was never
// read for display anywhere under `frontend/src/surface/`. Restored via
// `leaf/Timestamp.tsx` (shared `utils/timestamp.ts#fmtNodeTimestamp`,
// reusing the exact "HH:MM:SS today, MM/DD HH:MM:SS older" algorithm
// legacy's `fmtTime` still uses), wired into TypedPromptView's own footer
// and ResultView (the turn's terminal row) — the same two restoration
// points the parity audit calls out.

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Timestamp } from "../../src/surface/leaf/Timestamp";
import { ResultView } from "../../src/surface/nodes/Result";
import { TypedPromptView } from "../../src/surface/nodes/TypedPrompt";
import { fmtNodeTimestamp } from "../../src/utils/timestamp";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("fmtNodeTimestamp", () => {
  it("formats today's timestamp as bare HH:MM:SS", () => {
    const now = new Date();
    const seconds = now.getTime() / 1000;
    const expected = now.toLocaleTimeString(undefined, { hour12: false });
    expect(fmtNodeTimestamp(seconds)).toBe(expected);
  });

  it("formats an older timestamp with an MM/DD date prefix", () => {
    const old = new Date(2020, 0, 15, 9, 30, 0);
    const seconds = old.getTime() / 1000;
    expect(fmtNodeTimestamp(seconds)).toBe(
      `01/15 ${old.toLocaleTimeString(undefined, { hour12: false })}`,
    );
  });
});

describe("Timestamp leaf", () => {
  it("renders the legacy .message-box-footer/.user-message-time shape", () => {
    const { container } = render(<Timestamp ts={Date.now() / 1000} />);
    const footer = container.querySelector(".message-box-footer");
    expect(footer).not.toBeNull();
    expect(footer!.querySelector(".user-message-time")?.textContent).toBeTruthy();
  });
});

describe("TypedPromptView renders its own timestamp footer", () => {
  it("shows the prompt's ts as a formatted footer", () => {
    const ts = Date.now() / 1000;
    const n = node({
      node_id: "p1",
      turn_id: "t1",
      kind: "typed_prompt",
      ts,
      payload: {
        text: "hi",
        attachments: [],
        send_mode: "queue",
        origin: "user",
        source_session_ref: null,
        sent_text: null,
        intent_id: null,
      },
    });
    const { container } = render(<TypedPromptView node={n} />);
    const footer = container.querySelector(".message-box-footer .user-message-time");
    expect(footer?.textContent).toBe(fmtNodeTimestamp(ts));
  });
});

describe("ResultView renders the turn's terminal-row timestamp", () => {
  it("shows the result node's ts as a formatted footer", () => {
    const ts = Date.now() / 1000;
    const n = node({
      node_id: "r1",
      turn_id: "t1",
      kind: "result",
      ts,
      payload: { result_kind: "provider", text: "done", is_error: false },
    });
    render(<ResultView node={n} />);
    const result = screen.getByTestId("surface-result");
    expect(result.querySelector(".message-box-footer .user-message-time")?.textContent).toBe(
      fmtNodeTimestamp(ts),
    );
  });
});
