import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../src/types";
import { hasPendingPromptAck } from "../src/utils/pendingMessages";

function pendingMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "pending-message",
    role: "user",
    content: "prompt",
    events: [],
    timestamp: "2026-07-29T00:00:00.000Z",
    isStreaming: false,
    ...overrides,
  };
}

describe("pending prompt acknowledgement bridge", () => {
  it("marks an unacknowledged prompt as active", () => {
    expect(hasPendingPromptAck([pendingMessage()])).toBe(true);
  });

  it("does not treat file-discussion drafts as session runs", () => {
    expect(
      hasPendingPromptAck([
        pendingMessage({ file_discussion_id: "discussion" }),
      ]),
    ).toBe(false);
  });
});
