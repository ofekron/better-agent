import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, waitFor } from "@testing-library/react";
import React from "react";
import "../src/i18n";
import { Chat } from "../src/components/Chat";
import { makeSession, makeUserMsg } from "./fixtures";
import type { ChatMessage } from "../src/types";

afterEach(cleanup);

function renderChat(messages: ChatMessage[], pendingMessages: ChatMessage[]) {
  return render(
    <Chat
      messages={messages}
      pendingMessages={pendingMessages}
      runs={[]}
      streamingEvents={[]}
      traceSteps={[]}
      isStreaming={false}
      isStopping={false}
      streamingLoadPhase={null}
      onSend={() => true}
      disabled={false}
      session={makeSession()}
      draft=""
      onDraftChange={() => {}}
      queuedPrompt={null}
      onPromoteQueued={() => {}}
    />,
  );
}

describe("turn group identity across user_message_persisted ack", () => {
  it("does not remount the turn group when the pending message is swapped for the persisted one", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    try {
      const pending = makeUserMsg({
        id: "pending-123",
        content: "the prompt",
        status: "sending",
      });
      const { container, rerender } = renderChat([], [pending]);

      await waitFor(() => {
        expect(container.querySelector(".user-message-box")).not.toBeNull();
      });
      const nodeBefore = container.querySelector(".user-message-box") as HTMLElement;

      // Backend ack: persisted message carries the pending id as client_id.
      const persisted = makeUserMsg({
        id: "m-backend-1",
        client_id: "pending-123",
        content: "the prompt",
        seq: 1,
      });
      rerender(
        <Chat
          messages={[persisted]}
          pendingMessages={[]}
          runs={[]}
          streamingEvents={[]}
          traceSteps={[]}
          isStreaming={false}
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession()}
          draft=""
          onDraftChange={() => {}}
          queuedPrompt={null}
          onPromoteQueued={() => {}}
        />,
      );

      await waitFor(() => {
        expect(container.querySelector(".user-message-box")).not.toBeNull();
      });
      const nodeAfter = container.querySelector(".user-message-box") as HTMLElement;
      // Same DOM node: the TurnGroup was reconciled in place, not remounted.
      expect(nodeAfter).toBe(nodeBefore);
      expect(nodeBefore.isConnected).toBe(true);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});
