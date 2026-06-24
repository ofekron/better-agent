import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../src/components/Chat";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

describe("ShortcutResponses", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows shortcuts after Codex-style agent_message assistant text", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ shortcuts: ["TLDR"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <Chat
        messages={[
          makeUserMsg({ id: "u1", content: "what changed?" }),
          makeAssistantMsg({
            id: "a1",
            events: [
              {
                type: "agent_message",
                data: {
                  type: "assistant",
                  message: {
                    role: "assistant",
                    content: [{ type: "text", text: "Implemented the fix." }],
                  },
                  uuid: "agent-1",
                  timestamp: "2026-06-13T09:00:00.000Z",
                },
              },
            ],
          }),
        ]}
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
        shortcutResponses={["TLDR"]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "TLDR" })).toBeTruthy();
    });
  });
});
