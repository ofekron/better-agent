import { describe, expect, it } from "vitest";
import { mergeChatTreeDeltaMessage } from "../src/hooks/useSession";
import type { ChatMessage } from "../src/types";

function assistantMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "a",
    role: "assistant",
    content: "",
    events: [],
    isStreaming: false,
    ...overrides,
  };
}

describe("mergeChatTreeDeltaMessage", () => {
  it("seeds isStreaming=true for a brand-new message during the streaming phase", () => {
    const incoming = assistantMessage({ content: "hi", isStreaming: false });
    const merged = mergeChatTreeDeltaMessage(undefined, incoming, "streaming");
    expect(merged.isStreaming).toBe(true);
    expect(merged.content).toBe("hi");
  });

  it("seeds isStreaming=false for a brand-new message on a settle phase", () => {
    const incoming = assistantMessage({ content: "done", isStreaming: false });
    const merged = mergeChatTreeDeltaMessage(undefined, incoming, "settled");
    expect(merged.isStreaming).toBe(false);
  });

  it("preserves the current message's isStreaming across a streaming update", () => {
    const current = assistantMessage({ content: "hi", isStreaming: true });
    const incoming = assistantMessage({ content: "hi there", isStreaming: false });
    const merged = mergeChatTreeDeltaMessage(current, incoming, "streaming");
    expect(merged.isStreaming).toBe(true);
    expect(merged.content).toBe("hi there");
  });

  it("never lets a settle delta clobber stopped_at/isDetached already stamped by the lifecycle path", () => {
    // Simulates the raw turn_stopped frame (markTurnTerminal) winning the
    // race and stamping the message BEFORE this turn's settle delta lands.
    const current = assistantMessage({
      content: "hi there",
      isStreaming: false,
      stopped_at: "2026-07-16T00:00:00.000Z",
      interrupted_by_msg_id: "u2",
    });
    const incoming = assistantMessage({ content: "hi there", isStreaming: false });
    const merged = mergeChatTreeDeltaMessage(current, incoming, "stopped");
    expect(merged.stopped_at).toBe("2026-07-16T00:00:00.000Z");
    expect(merged.interrupted_by_msg_id).toBe("u2");
  });

  it("preserves isDetached across a settle delta arriving after markTurnDetached", () => {
    const current = assistantMessage({
      content: "partial",
      isStreaming: false,
      isDetached: true,
    });
    const incoming = assistantMessage({ content: "partial", isStreaming: false });
    const merged = mergeChatTreeDeltaMessage(current, incoming, "detached");
    expect(merged.isDetached).toBe(true);
  });

  it("always takes the incoming content/events over the current message", () => {
    const current = assistantMessage({ content: "old", isStreaming: true });
    const incoming = assistantMessage({
      content: "new and longer",
      isStreaming: false,
      events: [{ type: "output", data: { uuid: "e1", output: "new and longer" } }],
    });
    const merged = mergeChatTreeDeltaMessage(current, incoming, "streaming");
    expect(merged.content).toBe("new and longer");
    expect(merged.events).toHaveLength(1);
  });
});
