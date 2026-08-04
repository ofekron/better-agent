import { describe, expect, it, vi } from "vitest";

import { routeAppWebSocketEvent } from "../src/lib/webSocketProjectors/router";
import type { WebSocketProjectorContext } from "../src/lib/webSocketProjectors/types";
import type { WSEvent } from "../src/types";

function context(
  callbacks: WebSocketProjectorContext["callbacks"] = {},
  focusedSessionId = "focused",
): WebSocketProjectorContext {
  return {
    callbacks,
    state: {
      currentAppSessionId: focusedSessionId,
      clientId: callbacks.clientId,
      streamingLoadPhase: null,
      setEvents: vi.fn(),
      setIsStreaming: vi.fn(),
      setStreamingPhase: vi.fn(),
      setStreamingLoadPhase: vi.fn(),
      setLastResult: vi.fn(),
      setStreamingAppSessionId: vi.fn(),
      applyTerminalEvent: vi.fn(),
    },
  };
}

describe("app WebSocket projectors", () => {
  it("owns only application projection", () => {
    const order: string[] = [];
    const callbacks = {
      onEventSeqAdvance: (sessionId: string, seq: number) => {
        order.push(`cursor:${sessionId}:${seq}`);
      },
      onAnyEvent: () => order.push("any"),
      onMessagesDelta: (sessionId: string) => order.push(`projection:${sessionId}`),
    };

    routeAppWebSocketEvent({
      type: "messages_delta",
      data: { app_session_id: "payload-owner", messages: [] },
      session_id: "trusted-owner",
      seq: 9,
    } as WSEvent, context(callbacks));

    expect(order).toEqual(["projection:payload-owner"]);
  });

  it("unwraps extension toasts without publishing the inner event name", () => {
    const onExtensionToast = vi.fn();
    routeAppWebSocketEvent({
      type: "extension_event",
      session_id: "session-a",
      seq: 4,
      data: {
        extension_id: "extension-a",
        event_name: "extension_toast",
        data: { message: "Finished", level: "success" },
      },
    } as WSEvent, context({ onExtensionToast }));

    expect(onExtensionToast).toHaveBeenCalledWith({
      sessionId: "session-a",
      message: "Finished",
      level: "success",
    });
  });

  it("dispatches prompt acknowledgement synchronously", () => {
    let acknowledged = false;
    routeAppWebSocketEvent({
      type: "user_message_persisted",
      data: { session_id: "session-a", user_message: { id: "message-a" } },
    } as WSEvent, context({
      onUserMessagePersisted: () => {
        acknowledged = true;
      },
    }));
    expect(acknowledged).toBe(true);
  });

  it("keeps ownerless live frames filtered and terminal fallback scoped", () => {
    const onLiveTurnEvent = vi.fn();
    const onTurnTerminal = vi.fn();
    const projectorContext = context({ onLiveTurnEvent, onTurnTerminal });

    routeAppWebSocketEvent({
      type: "agent_message",
      data: { type: "assistant" },
    } as WSEvent, projectorContext);
    routeAppWebSocketEvent({
      type: "turn_complete",
      data: { success: true },
    } as WSEvent, projectorContext);

    expect(onLiveTurnEvent).not.toHaveBeenCalled();
    expect(onTurnTerminal).toHaveBeenCalledWith("focused");
  });
});
