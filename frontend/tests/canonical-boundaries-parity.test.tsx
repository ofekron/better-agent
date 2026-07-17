import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render } from "@testing-library/react";
import React from "react";
import { describe, expect, it } from "vitest";
import { chatTreeToMessages, type ChatTreeLookupEntry } from "src/chat/chatTreeClient";
import { parseProjection } from "src/chat/parseProjection";
import { MessageBubble } from "src/components/MessageBubble";
import type { ChatMessage } from "src/types";

type FixtureMessage = { id: string; role: string; content: string; seq: number };
type FixtureEvent = {
  event_id: string;
  type: string;
  data: Record<string, unknown>;
  message_id: string | null;
  timestamp: string;
};
type Fixture = {
  messages: FixtureMessage[];
  events: FixtureEvent[];
  expected: { chat_tree_completed: unknown };
};

const fixture = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "../../test-contracts/chat-panel/v1/canonical-session.json"),
    "utf8",
  ),
) as Fixture;

/** Lookup sidecar mirroring the BFF wire for the fixture: message
 * entries from the session messages, event entries from the durable
 * log (last write wins for mutable event ids, as in the projector). */
function fixtureLookup(): Record<string, ChatTreeLookupEntry> {
  const lookup: Record<string, ChatTreeLookupEntry> = {};
  for (const message of fixture.messages) {
    lookup[message.id] = {
      kind: "message",
      role: message.role,
      text: message.content,
      seq: message.seq,
      snapshot: { id: message.id, role: message.role, content: message.content },
    };
  }
  for (const event of fixture.events) {
    lookup[event.event_id] = {
      kind: "event",
      type: event.type,
      data: event.data,
      message_id: event.message_id,
      timestamp: event.timestamp,
    };
  }
  return lookup;
}

const projection = parseProjection(fixture.expected.chat_tree_completed);
const lookup = fixtureLookup();
const messages = chatTreeToMessages(projection, lookup);

function assistant(id: string): ChatMessage {
  const message = messages.find((m) => m.id === id);
  if (!message) throw new Error(`fixture assistant ${id} missing`);
  return message;
}

function countOccurrences(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

describe("canonical boundary metadata production (chatTreeClient)", () => {
  it("carries turn-1's Explanation partitions and result boundary verbatim", () => {
    expect(assistant("a1").canonical_turn).toEqual({
      body: [
        {
          kind: "explanation",
          text: "I will inspect the inputs.",
          textEventIds: ["e-text-1a"],
          itemIds: ["e-tool-1"],
        },
        {
          kind: "explanation",
          text: "The inputs are consistent.",
          textEventIds: ["e-text-1b"],
          itemIds: [],
        },
      ],
      result: {
        type: "ProviderResult",
        partIds: ["e-final-card", "e-final-text"],
        textSourceIds: ["e-final-text"],
        text: "The report is ready.",
      },
    });
  });

  it("carries body item order including steering and scoped turns (turn-2)", () => {
    expect(assistant("a2").canonical_turn?.body.map((item) =>
      item.kind === "explanation" ? `explanation:${item.text}` : `${item.kind}:${item.id}`,
    )).toEqual([
      "explanation:Resolved after ownership arrived.",
      "explanation:Final replaced answer",
      "steering:e-steer",
      "scoped:e-ns1",
      "scoped:e-worker1",
    ]);
    expect(assistant("a2").canonical_turn?.result).toBeNull();
  });

  it("separates concatenated-text source ids from non-text result parts (turn-7)", () => {
    expect(assistant("a7").canonical_turn?.result).toEqual({
      type: "ProviderResult",
      partIds: ["e-ask-picker", "e-text-7a", "e-text-7b"],
      textSourceIds: ["e-text-7a", "e-text-7b"],
      text: "Session finished.",
    });
  });
});

describe("canonical boundary consumption (MessageBubble parity)", () => {
  it("renders turn-1 grouping equal to the canonical Explanation structure", () => {
    const { container } = render(<MessageBubble message={assistant("a1")} />);
    const canonical = assistant("a1").canonical_turn!;
    const expectedGroups = canonical.body.filter(
      (item) => item.kind === "explanation" && item.text && item.itemIds.length > 0,
    );
    const groups = container.querySelectorAll('[data-testid="auto-action-group"]');
    // Grouping comes from the projector's partitions — one collapsible
    // group per explanation partition that owns actions.
    expect(groups.length).toBe(expectedGroups.length);
    expect(groups.length).toBe(1);
    expect(groups[0].querySelector(".auto-action-group-lead")?.textContent)
      .toContain("I will inspect the inputs.");
    expect(groups[0].textContent).toContain("1 action");
    // The action-less partition renders as its own standalone text row,
    // outside any group.
    const text = container.textContent ?? "";
    expect(text).toContain("The inputs are consistent.");
    expect(groups[0].textContent).not.toContain("The inputs are consistent.");
  });

  it("renders the result exactly once — content box owns it, no event-tail duplicate", () => {
    const { container } = render(<MessageBubble message={assistant("a1")} />);
    const text = container.textContent ?? "";
    expect(countOccurrences(text, "The report is ready.")).toBe(1);
  });

  it("renders a multi-source result once instead of re-deriving per-chunk rows (turn-7)", () => {
    const { container } = render(<MessageBubble message={assistant("a7")} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Session finished.");
    // Pre-canonical rendering emitted the raw chunks ("Session " +
    // "finished.") as body rows AND the content box — duplication.
    expect(countOccurrences(text, "finished.")).toBe(1);
  });

  it("keeps deriving heuristically while the turn is still streaming", () => {
    const streaming: ChatMessage = { ...assistant("a7"), isStreaming: true };
    const { container } = render(<MessageBubble message={streaming} />);
    const text = container.textContent ?? "";
    // Live path: the raw chunk rows render (no canonical consumption yet).
    expect(countOccurrences(text, "finished.")).toBeGreaterThanOrEqual(1);
    // Completed content box is not rendered while streaming.
    expect(container.querySelector('[data-canonical-message-text]')).not.toBeNull();
  });
});
