import { describe, expect, it } from "vitest";
import { sessionMessageCount } from "../src/lib/sessionMessageCount";

describe("sessionMessageCount", () => {
  it("uses backend-owned count when present", () => {
    expect(sessionMessageCount({ message_count: 7, messages: [] })).toBe(7);
  });

  it("falls back to user messages only", () => {
    expect(
      sessionMessageCount({
        messages: [
          { id: "u1", role: "user", content: "hello" },
          { id: "a1", role: "assistant", content: "", isStreaming: true },
          { id: "u2", role: "user", content: "again" },
          { id: "a2", role: "assistant", content: "done" },
        ],
      }),
    ).toBe(2);
  });
});
