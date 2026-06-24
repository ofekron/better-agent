import { describe, it, expect } from "vitest";
import type { ChatMessage, WSEvent } from "../src/types";
import { resolveLiveEventTargetIndex } from "../src/hooks/useSession";

/** Minimal assistant/user messages — the resolver only reads
 * role / id / isStreaming. */
function user(id: string): ChatMessage {
  return { id, role: "user", content: "", events: [], timestamp: "", isStreaming: false };
}
function asst(id: string, streaming = false): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    events: [],
    timestamp: "",
    isStreaming: streaming,
  };
}
function agentFrame(msgId?: string): WSEvent {
  const data: Record<string, unknown> = { type: "assistant" };
  if (msgId) data.msg_id = msgId;
  return { type: "agent_message", data };
}

describe("resolveLiveEventTargetIndex", () => {
  it("routes a late event to its owning msg_id (no duplicate placeholder)", () => {
    // The "last message duplicated" bug: a finalized assistant a1, no
    // active run, no streaming assistant. Without msg_id routing this
    // returns -1 (→ placeholder bubble = the duplicate). With the fix it
    // routes to a1.
    const msgs = [user("u"), asst("a1")];
    expect(resolveLiveEventTargetIndex(msgs, agentFrame("a1"), null)).toBe(1);
  });

  it("msg_id wins over a different active run target (prevents grafting onto a newer turn)", () => {
    // A late event owns finalized a1; a newer turn's run target points at
    // a not-yet-created a2. Routing by msg_id keeps it on a1.
    const msgs = [user("u1"), asst("a1"), user("u2")];
    expect(
      resolveLiveEventTargetIndex(msgs, agentFrame("a1"), "a2"),
    ).toBe(1);
  });

  it("falls back to the active run target when the frame has no msg_id", () => {
    const msgs = [user("u"), asst("a1", true)];
    expect(resolveLiveEventTargetIndex(msgs, agentFrame(), "a1")).toBe(1);
  });

  it("falls back to the last streaming assistant when neither msg_id nor run target resolve", () => {
    const msgs = [user("u"), asst("a1", true)];
    expect(resolveLiveEventTargetIndex(msgs, agentFrame(), null)).toBe(1);
  });

  it("returns -1 only for a genuinely new turn (no msg_id, no run, no streaming)", () => {
    const msgs = [user("u"), asst("a1")];
    expect(resolveLiveEventTargetIndex(msgs, agentFrame(), null)).toBe(-1);
  });

  it("returns -1 when msg_id names a message that is not present", () => {
    const msgs = [user("u"), asst("a1")];
    expect(resolveLiveEventTargetIndex(msgs, agentFrame("ghost"), null)).toBe(-1);
  });
});
