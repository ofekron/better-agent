import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import "../src/i18n";
import { Chat } from "../src/components/Chat";
import { TurnGroup } from "../src/components/MessageBubble";
import { chatTreeToMessages, type ChatTreeLookupEntry } from "../src/chat/chatTreeClient";
import { parseProjection } from "../src/chat/parseProjection";
import { makeSession } from "./fixtures";

afterEach(cleanup);

const items = [
  {
    type: "Turn",
    id: "u-old",
    prompt: "u-old",
    body: [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["tool-old"] }],
    result: { type: "ProviderResult", part_ids: ["out-old"], text: "Older done." },
  },
  {
    type: "Turn",
    id: "u-last",
    prompt: "u-last",
    body: [],
    result: { type: "ProviderResult", part_ids: ["out-last"], text: "Latest done." },
  },
];

const lookup: Record<string, ChatTreeLookupEntry> = {
  "u-old": { kind: "message", role: "user", text: "older prompt", seq: 0, snapshot: { id: "u-old", role: "user", seq: 0 } },
  "a-old": { kind: "message", role: "assistant", text: "", seq: 1, snapshot: { id: "a-old", role: "assistant", seq: 1 } },
  "tool-old": { kind: "event", type: "tool_interaction", data: { tool_name: "Bash", tool_use_id: "tool-1", status: "running" }, message_id: "a-old" },
  "out-old": { kind: "event", type: "assistant_text", data: { text: "Older done." }, message_id: "a-old" },
  "u-last": { kind: "message", role: "user", text: "latest prompt", seq: 2, snapshot: { id: "u-last", role: "user", seq: 2 } },
  "a-last": { kind: "message", role: "assistant", text: "", seq: 3, snapshot: { id: "a-last", role: "assistant", seq: 3 } },
  "out-last": { kind: "event", type: "assistant_text", data: { text: "Latest done." }, message_id: "a-last" },
};

function messagesFromTree() {
  return chatTreeToMessages(parseProjection(items), lookup);
}

describe("cold chat-tree rendering", () => {
  it("renders formal-tree prompts and assistants in order on cold load", () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    try {
      const { container } = render(
        <Chat
          messages={messagesFromTree()}
          pendingMessages={[]}
          runs={[]}
          streamingEvents={[]}
          isStreaming={false}
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession({ id: "s" })}
          draft=""
          onDraftChange={() => {}}
          onPromoteQueued={() => {}}
        />,
      );

      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-testid="user-message"], [data-testid="assistant-message"]'))
          .map((element) => element.dataset.messageId),
      ).toEqual(["u-old", "a-old", "u-last", "a-last"]);
      expect(container.textContent).toContain("older prompt");
      expect(container.textContent).toContain("latest prompt");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it.each([false, true])(
    "does not duplicate final assistant text when result events are attached and collapsed=%s",
    (defaultCollapsed) => {
      const messages = messagesFromTree();
      const { container } = render(
        <TurnGroup
          initiatorMessage={messages[2]}
          responseMessage={messages[3]}
          defaultCollapsed={defaultCollapsed}
          orchestrationMode="native"
        />,
      );

      const matches = container.textContent?.match(/Latest done\./g) ?? [];
      expect(matches).toHaveLength(1);
    },
  );
});
