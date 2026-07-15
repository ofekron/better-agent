import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  type MessageProjectionOperations,
  useMessageProjectionEvents,
} from "../src/hooks/useMessageProjectionEvents";
import { eventBus } from "../src/lib/eventBus";

function operations(): MessageProjectionOperations {
  return {
    applyRecovering: vi.fn(),
    applyRetrying: vi.fn(),
    applyAutoRetry: vi.fn(),
    applyContent: vi.fn(),
    applyContinuation: vi.fn(),
    applyRunMeta: vi.fn(),
    applyAskResult: vi.fn(),
    applyAskChoice: vi.fn(),
    applySessionProcessing: vi.fn(),
  };
}

describe("message projection event controller", () => {
  it("normalizes message projection facts into narrow operations", () => {
    const ops = operations();
    renderHook(() => useMessageProjectionEvents(ops));

    act(() => {
      eventBus.publish("message_recovering_changed", {
        session_id: "session-1",
        msg_id: "message-1",
        value: true,
      });
      eventBus.publish("message_retrying_changed", {
        session_id: "session-1",
        msg_id: "message-1",
        retry_at: "2026-07-15T12:00:00Z",
        error_text: "rate limited",
      });
      eventBus.publish("message_content_updated", {
        session_id: "session-1",
        msg_id: "message-1",
      });
      eventBus.publish("message_continuation_changed", {
        session_id: "session-1",
        msg_id: "message-1",
      });
      eventBus.publish("message_ask_choice_changed", {
        session_id: "session-1",
        msg_id: "message-1",
      });
    });

    expect(ops.applyRecovering).toHaveBeenCalledWith("session-1", "message-1", true);
    expect(ops.applyRetrying).toHaveBeenCalledWith(
      "session-1",
      "message-1",
      "2026-07-15T12:00:00Z",
      "rate limited",
    );
    expect(ops.applyContent).toHaveBeenCalledWith("session-1", "message-1", "");
    expect(ops.applyContinuation).toHaveBeenCalledWith("session-1", "message-1", null);
    expect(ops.applyAskChoice).toHaveBeenCalledWith("session-1", "message-1", null);
  });

  it("routes processing states, rejects incomplete identities, and detaches", () => {
    const ops = operations();
    const { unmount } = renderHook(() => useMessageProjectionEvents(ops));

    act(() => {
      eventBus.publish("session_processing_started", { root_id: "root-1" });
      eventBus.publish("session_processing_finished", { root_id: "root-1" });
      eventBus.publish("message_content_updated", { session_id: "session-1" });
    });

    expect(ops.applySessionProcessing).toHaveBeenNthCalledWith(1, "root-1", "started");
    expect(ops.applySessionProcessing).toHaveBeenNthCalledWith(2, "root-1", "finished");
    expect(ops.applyContent).not.toHaveBeenCalled();

    unmount();
    eventBus.publish("session_processing_started", { root_id: "root-1" });
    expect(ops.applySessionProcessing).toHaveBeenCalledTimes(2);
  });
});
