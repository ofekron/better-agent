import { describe, it, expect } from "vitest";
import { oldestNumericSeq } from "../src/utils/mergeMessages";
import type { ChatMessage } from "../src/types";

const msg = (seq: number | undefined, id: string): ChatMessage =>
  ({ id, role: "assistant", content: "", seq }) as ChatMessage;

describe("oldestNumericSeq", () => {
  it("returns the smallest seq", () => {
    expect(oldestNumericSeq([msg(6, "a"), msg(9, "b"), msg(11, "c")])).toBe(6);
  });

  it("ignores live/streaming placeholders that have no seq", () => {
    // Regression: a `live-*` optimistic assistant turn carries no seq.
    // The old `m.seq ?? 0` reduce folded it to 0 and silently disabled
    // load-older for the whole thread.
    expect(
      oldestNumericSeq([msg(6, "a"), msg(9, "b"), msg(undefined, "live-1")]),
    ).toBe(6);
  });

  it("returns null when no message has a numeric seq", () => {
    expect(oldestNumericSeq([msg(undefined, "live-1")])).toBeNull();
    expect(oldestNumericSeq([])).toBeNull();
  });
});
