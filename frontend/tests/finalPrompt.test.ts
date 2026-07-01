import { describe, expect, it } from "vitest";
import { buildFinalPrompt } from "../src/utils/finalPrompt";
import type { InlineTag } from "../src/types/inlineTag";

const tag: InlineTag = {
  id: "tag-1",
  messageId: "msg-1",
  selectedText: "selected text",
  comment: "queued comment",
  timestamp: "2026-07-01T00:00:00.000Z",
};

describe("buildFinalPrompt", () => {
  it("adds inline comments to normal prompts", () => {
    const result = buildFinalPrompt({
      prompt: "do the work",
      tags: [tag],
      sendMode: "interrupt",
    });

    expect(result.sendMode).toBe("interrupt");
    expect(result.prompt).toContain("<inline-tags>");
    expect(result.prompt).toContain("queued comment");
    expect(result.prompt).toContain("do the work");
  });

  it("shares queued-comment merge semantics with regular send", () => {
    const result = buildFinalPrompt({
      prompt: "extra instruction",
      tags: [tag],
      sendMode: "queue",
      latestQueued: { preview: "queued work" },
    });

    expect(result.sendMode).toBe("alter");
    expect(result.prompt).toContain("queued work");
    expect(result.prompt).toContain("queued comment");
    expect(result.prompt).toContain("extra instruction");
  });
});
