import { describe, expect, it } from "vitest";
import {
  chatTreeToMessages,
  type ChatTreeLookupEntry,
} from "src/chat/chatTreeClient";
import { parseProjection } from "src/chat/parseProjection";
import { mergeChatTreeDeltaMessage } from "src/hooks/useSession";
import type { ChatMessage } from "src/types";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

const MANIFEST = {
  id: "a1",
  type: "turn",
  revision: "hr-32",
  direct_child_count: 3,
  display_summary: "3 steps",
};

function turnWire(): unknown[] {
  return [
    {
      type: "Turn",
      id: "u1",
      prompt: "u1",
      body: [],
      result: { type: "ProviderResult", part_ids: ["a1:final"], text: "done" },
    },
  ];
}

function lookupWire(
  assistantExtras: Record<string, unknown> = {},
): Record<string, ChatTreeLookupEntry> {
  return {
    u1: {
      kind: "message",
      role: "user",
      text: "hi",
      seq: 1,
      snapshot: { id: "u1", role: "user", content: "hi" },
    },
    "a1:final": {
      kind: "event",
      type: "assistant_text",
      data: { text: "done" },
      message_id: "a1",
    },
    a1: {
      kind: "message",
      role: "assistant",
      text: "done",
      seq: 2,
      snapshot: { id: "a1", role: "assistant", content: "done" },
      ...assistantExtras,
    } as ChatTreeLookupEntry,
  };
}

function adapt(assistantExtras: Record<string, unknown> = {}): ChatMessage[] {
  return chatTreeToMessages(parseProjection(turnWire()), lookupWire(assistantExtras));
}

describe("historical_hydration_root chat-tree wire mapping", () => {
  it("maps the lookup message entry's top-level manifest onto the adapted assistant message", () => {
    const messages = adapt({ historical_hydration_root: MANIFEST });
    const assistant = messages.find((m) => m.id === "a1");
    expect(assistant?.historical_hydration_root).toEqual(MANIFEST);
  });

  it("leaves the field unset when the wire omits it", () => {
    const assistant = adapt().find((m) => m.id === "a1");
    expect(assistant).toBeDefined();
    expect("historical_hydration_root" in (assistant as ChatMessage)).toBe(false);
  });

  it("preserves an explicit null (no historical work)", () => {
    const assistant = adapt({ historical_hydration_root: null }).find((m) => m.id === "a1");
    expect(assistant?.historical_hydration_root).toBeNull();
  });

  it("fails closed on malformed manifests: the gate stays off", () => {
    for (const bad of [
      { id: "a1" },
      { ...MANIFEST, direct_child_count: "3" },
      { ...MANIFEST, revision: 32 },
      "not-an-object",
      ["array"],
      7,
    ]) {
      const assistant = adapt({ historical_hydration_root: bad }).find((m) => m.id === "a1");
      expect(assistant?.historical_hydration_root).toBeUndefined();
    }
  });

  it("prefers the explicit wire field over a stale snapshot passthrough copy", () => {
    const messages = chatTreeToMessages(
      parseProjection(turnWire()),
      lookupWire({
        historical_hydration_root: MANIFEST,
      }),
    );
    const withSnapshotCopy = chatTreeToMessages(
      parseProjection(turnWire()),
      (() => {
        const lookup = lookupWire({ historical_hydration_root: MANIFEST });
        const entry = lookup.a1;
        if (entry.kind === "message") {
          entry.snapshot = {
            ...entry.snapshot,
            historical_hydration_root: { ...MANIFEST, revision: "hr-STALE" },
          };
        }
        return lookup;
      })(),
    );
    expect(messages.find((m) => m.id === "a1")?.historical_hydration_root)
      .toEqual(MANIFEST);
    expect(withSnapshotCopy.find((m) => m.id === "a1")?.historical_hydration_root)
      .toEqual(MANIFEST);
  });

  it("arms the historical-work gate end-to-end from the chat-tree wire", async () => {
    const session = makeSession({
      id: "hist-1",
      messages: [
        makeUserMsg({ id: "u1", seq: 0, content: "do work" }),
        makeAssistantMsg({
          id: "a1",
          seq: 1,
          content: "done",
          historical_hydration_root: { ...MANIFEST },
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("hist-1");
    expect(h.$(".historical-process-toggle")).not.toBeNull();
    h.unmount();
  });

  it("survives the chat_tree_delta merge unchanged", () => {
    const incoming = adapt({ historical_hydration_root: MANIFEST })
      .find((m) => m.id === "a1")!;
    const current: ChatMessage = {
      id: "a1",
      role: "assistant",
      content: "",
      events: [],
      isStreaming: false,
    };
    const merged = mergeChatTreeDeltaMessage(current, incoming, "settled");
    expect(merged.historical_hydration_root).toEqual(MANIFEST);
  });
});
