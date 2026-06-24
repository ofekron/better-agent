import { describe, expect, it } from "vitest";
import { mergeMessagesSorted } from "../src/utils/mergeMessages";
import type { ChatMessage } from "../src/types";

function userMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "message",
    role: "user",
    content: "message",
    events: [],
    timestamp: "2026-06-17T10:00:00.000Z",
    isStreaming: false,
    ...overrides,
  };
}

describe("mergeMessagesSorted", () => {
  it("drops an optimistic pending message once its persisted client_id exists", () => {
    const pending = userMessage({
      id: "pending-1",
      status: "error",
      errorText: "HTTP 500",
    });
    const persisted = userMessage({
      id: "persisted-1",
      client_id: "pending-1",
      lifecycle_msg_id: "lifecycle-1",
    });

    expect(mergeMessagesSorted([persisted], [pending])).toEqual([persisted]);
  });

  it("keeps unacknowledged pending messages", () => {
    const pending = userMessage({ id: "pending-1" });
    const persisted = userMessage({
      id: "persisted-1",
      client_id: "pending-2",
    });

    expect(mergeMessagesSorted([persisted], [pending])).toEqual([
      persisted,
      pending,
    ]);
  });
});
