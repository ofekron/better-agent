import { describe, expect, it } from "vitest";
import {
  compareRenderedTreeToSession,
  sessionIsStreaming,
  messageTextCandidates,
  type RenderedTree,
} from "../src/lib/staleViewDetector";
import type { Session, ChatMessage } from "../src/types";

const at = () => new Date("2024-01-01T00:00:00.000Z");

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "x",
    role: "user",
    content: "",
    events: [],
    timestamp: "",
    isStreaming: false,
    ...over,
  } as ChatMessage;
}

function linearSession(): Session {
  return {
    id: "root",
    messages: [
      msg({ id: "u1", role: "user", seq: 1, content: "hello" }),
      msg({ id: "a1", role: "assistant", seq: 2, content: "answer **one**" }),
      msg({ id: "u2", role: "user", seq: 3, content: "next prompt" }),
      msg({ id: "a2", role: "assistant", seq: 4, content: "answer two" }),
    ],
  } as unknown as Session;
}

function forkSession(): Session {
  return {
    id: "root",
    fork_point_seq: null,
    messages: [
      msg({ id: "u1", role: "user", seq: 1, content: "hello" }),
      msg({ id: "a1", role: "assistant", seq: 2, content: "answer one" }),
    ],
    forks: [
      {
        id: "fork",
        fork_point_seq: 2,
        messages: [
          msg({ id: "u1", role: "user", seq: 1, content: "hello" }),
          msg({ id: "a1", role: "assistant", seq: 2, content: "answer one" }),
          msg({ id: "uf", role: "user", seq: 3, content: "fork prompt" }),
          msg({ id: "af", role: "assistant", seq: 4, content: "fork answer" }),
        ],
        forks: [],
      },
    ],
  } as unknown as Session;
}

function tree(regions: RenderedTree["regions"]): RenderedTree {
  return { visible: true, session_id: "root", title: null, regions };
}

describe("compareRenderedTreeToSession", () => {
  it("accepts a rendered in-order subsequence of the linear session", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [
            { id: "u1", role: "user", text: "hello" },
            { id: "u2", role: "user", text: "next prompt" },
          ],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(true);
    expect(r.mismatches).toEqual([]);
  });

  it("accepts markdown-stripped rendered text (answer one) vs raw content (answer **one**)", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [{ id: "a1", role: "assistant", text: "answer one" }],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(true);
  });

  it("flags an unexpected message not in the canonical snapshot (stale extra bubble)", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [
            { id: "u1", role: "user", text: "hello" },
            { id: "ghost", role: "assistant", text: "leftover" },
          ],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches.some((m) => m.kind === "unexpected_message" && m.message_id === "ghost")).toBe(true);
  });

  it("flags out-of-order rendered messages (reordered view)", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [
            { id: "u2", role: "user", text: "next prompt" },
            { id: "u1", role: "user", text: "hello" },
          ],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches.some((m) => m.kind === "out_of_order")).toBe(true);
  });

  it("flags a role mismatch", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [{ id: "u1", role: "assistant", text: "hello" }],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches.some((m) => m.kind === "role_mismatch")).toBe(true);
  });

  it("flags text that does not match canonical content", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "linear",
          session_id: "root",
          messages: [{ id: "u1", role: "user", text: "totally different text" }],
        },
      ]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches.some((m) => m.kind === "text_mismatch")).toBe(true);
  });

  it("reports panel-not-visible as a skip, not a failure", () => {
    const r = compareRenderedTreeToSession(
      { visible: false, session_id: "root", title: null, regions: [] },
      linearSession(),
      at,
    );
    expect(r.ok).toBe(true);
    expect(r.skipped).toBe(true);
  });

  it("fails when the extractor returned no tree", () => {
    const r = compareRenderedTreeToSession(null, linearSession(), at);
    expect(r.ok).toBe(false);
    expect(r.mismatches[0].kind).toBe("extractor_missing");
  });

  it("fails when the canonical snapshot is missing", () => {
    const r = compareRenderedTreeToSession(
      tree([{ kind: "linear", session_id: "root", messages: [] }]),
      null,
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches[0].kind).toBe("no_session_snapshot");
  });

  it("fails when a visible panel has no regions", () => {
    const r = compareRenderedTreeToSession(tree([]), linearSession(), at);
    expect(r.ok).toBe(false);
    expect(r.mismatches[0].kind).toBe("no_regions");
  });

  it("flags a region whose session_id resolves to no node", () => {
    const r = compareRenderedTreeToSession(
      tree([{ kind: "fork_pane", session_id: "nope", messages: [] }]),
      linearSession(),
      at,
    );
    expect(r.ok).toBe(false);
    expect(r.mismatches[0].kind).toBe("region_no_node");
  });

  it("accepts a valid fork layout: shared region above the split + per-pane below", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "fork_shared",
          session_id: "root",
          messages: [
            { id: "u1", role: "user", text: "hello" },
            { id: "a1", role: "assistant", text: "answer one" },
          ],
        },
        {
          kind: "fork_pane",
          session_id: "fork",
          messages: [
            { id: "uf", role: "user", text: "fork prompt" },
            { id: "af", role: "assistant", text: "fork answer" },
          ],
        },
      ]),
      forkSession(),
      at,
    );
    expect(r.ok).toBe(true);
  });

  it("flags a fork pane that renders a shared (above-split) message it should not", () => {
    const r = compareRenderedTreeToSession(
      tree([
        {
          kind: "fork_pane",
          session_id: "fork",
          messages: [{ id: "u1", role: "user", text: "hello" }],
        },
      ]),
      forkSession(),
      at,
    );
    // u1 is a shared/above-split message, not part of the pane's below-split set.
    expect(r.ok).toBe(false);
    expect(r.mismatches.some((m) => m.kind === "unexpected_message")).toBe(true);
  });
});

describe("sessionIsStreaming", () => {
  it("is true when any assistant message is streaming", () => {
    const s = linearSession();
    (s.messages![3] as ChatMessage).isStreaming = true;
    expect(sessionIsStreaming(s)).toBe(true);
  });
  it("is true when a fork is streaming", () => {
    const s = forkSession();
    (s.forks![0].messages![3] as ChatMessage).isStreaming = true;
    expect(sessionIsStreaming(s)).toBe(true);
  });
  it("is false for a settled session", () => {
    expect(sessionIsStreaming(linearSession())).toBe(false);
  });
  it("is false for null", () => {
    expect(sessionIsStreaming(null)).toBe(false);
  });
});

describe("messageTextCandidates", () => {
  it("includes assistant event text from agent_message frames", () => {
    const m = msg({
      id: "a",
      role: "assistant",
      content: "",
      events: [
        {
          type: "agent_message",
          data: { type: "assistant", message: { content: [{ type: "text", text: "from event" }] } },
        } as unknown as ChatMessage["events"][number],
      ],
    });
    expect(messageTextCandidates(m)).toContain("from event");
  });
});
