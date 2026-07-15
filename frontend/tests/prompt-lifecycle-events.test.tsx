import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type PromptLifecycleOperations,
  usePromptLifecycleEvents,
} from "../src/hooks/usePromptLifecycleEvents";
import { eventBus } from "../src/lib/eventBus";

vi.mock("../src/lib/promptSendLog", () => ({ logPromptSend: vi.fn() }));

function operations(): PromptLifecycleOperations {
  return {
    getFocusedSessionId: vi.fn(() => "focused-session"),
    pendingDraftCount: vi.fn(() => 2),
    takePendingDraft: vi.fn(() => ({
      id: "optimistic-id",
      clientId: "client-1",
      preview: "full pending text",
    })),
    acknowledgeQueue: vi.fn(),
    consumeQueue: vi.fn(),
    clearOfflineDispatch: vi.fn(),
    removeOfflineAction: vi.fn(),
    removePending: vi.fn(),
    stampPendingLifecycle: vi.fn(),
    patchMessageStatus: vi.fn(),
    markPendingFailed: vi.fn(),
  };
}

describe("prompt lifecycle event controller", () => {
  let ops: PromptLifecycleOperations;

  beforeEach(() => {
    ops = operations();
  });

  it("acknowledges queued prompts from the optimistic draft and clears its bridge", () => {
    renderHook(() => usePromptLifecycleEvents(ops));

    act(() => eventBus.publish("prompt_queued", {
      app_session_id: "session-1",
      queued_id: "queue-1",
      prompt_preview: "truncated",
      client_id: "client-1",
      queue_revision: 7,
    }));

    expect(ops.acknowledgeQueue).toHaveBeenCalledWith("session-1", {
      id: "queue-1",
      clientId: "client-1",
      preview: "full pending text",
    }, 7);
    expect(ops.clearOfflineDispatch).toHaveBeenCalledWith("session-1", "client-1");
    expect(ops.removeOfflineAction).toHaveBeenCalledWith("session-1", "client-1");
    expect(ops.removePending).toHaveBeenCalledWith("client-1");
  });

  it("projects lifecycle states and uses the focused session fallback", () => {
    renderHook(() => usePromptLifecycleEvents(ops));

    act(() => {
      eventBus.publish("user_message_sent", { lifecycle_msg_id: "message-1" });
      eventBus.publish("user_message_failed", {
        app_session_id: "session-2",
        lifecycle_msg_id: "message-2",
        reason: "persist failed",
      });
    });

    expect(ops.patchMessageStatus).toHaveBeenNthCalledWith(
      1,
      "focused-session",
      "message-1",
      "sending",
    );
    expect(ops.patchMessageStatus).toHaveBeenNthCalledWith(
      2,
      "session-2",
      "message-2",
      "error",
      "persist failed",
    );
    expect(ops.markPendingFailed).toHaveBeenCalledWith("message-2", "persist failed");
  });

  it("handles queued-behind and queue-consumed facts, then detaches", () => {
    const { unmount } = renderHook(() => usePromptLifecycleEvents(ops));

    act(() => {
      eventBus.publish("user_message_queued", {
        app_session_id: "session-1",
        lifecycle_msg_id: "message-1",
        client_id: "client-1",
        kind: "queued_behind",
      });
      eventBus.publish("queue_consumed", {
        app_session_id: "session-1",
        queued_id: "queue-1",
      });
    });

    expect(ops.removePending).toHaveBeenCalledWith("client-1");
    expect(ops.stampPendingLifecycle).not.toHaveBeenCalled();
    expect(ops.consumeQueue).toHaveBeenCalledWith("session-1", ["queue-1"]);

    unmount();
    eventBus.publish("queue_consumed", { app_session_id: "session-1" });
    expect(ops.consumeQueue).toHaveBeenCalledTimes(1);
  });
});
