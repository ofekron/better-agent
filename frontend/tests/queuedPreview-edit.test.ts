import { describe, expect, it } from "vitest";
import { splitPreview, applyQueuedEdit, applyQueuedInlineTags } from "../src/utils/queuedPreview";

const TAGS = "<inline-tags><c file=\"a.ts\" range=\"1-2\">look here</c></inline-tags>";

describe("applyQueuedEdit — preserves the envelope when editing a tagged queued prompt", () => {
  it("re-attaches the inline-tags envelope instead of dropping it", () => {
    // Regression: editing a queued prompt that carried an <inline-tags>
    // envelope used to commit the bare edited user text, losing the tags.
    const preview = `${TAGS}\n\nmy question`;
    expect(applyQueuedEdit(preview, "my edited question")).toBe(
      `${TAGS}\n\nmy edited question`,
    );
  });

  it("preserves a system-reminder open-files preamble too", () => {
    const preamble = "<system-reminder>open files: a.ts</system-reminder>";
    const preview = `${preamble}\n${TAGS}\n\nfix the bug`;
    expect(applyQueuedEdit(preview, "fix it properly")).toBe(
      `${preamble}\n${TAGS}\n\nfix it properly`,
    );
  });

  it("handles user text with leading/trailing whitespace (trimmed display)", () => {
    const preview = `${TAGS}\n\n  spaced text  `;
    expect(splitPreview(preview).userText).toBe("spaced text");
    expect(applyQueuedEdit(preview, "new text")).toBe(`${TAGS}\n\nnew text`);
  });

  it("handles a tags-only queued prompt (empty user text)", () => {
    const preview = `${TAGS}\n\n`;
    expect(splitPreview(preview).userText).toBe("");
    // Adding text keeps the envelope as the prefix.
    expect(applyQueuedEdit(preview, "added").startsWith(TAGS)).toBe(true);
    expect(applyQueuedEdit(preview, "added").endsWith("added")).toBe(true);
  });

  it("does not collide when edited text matches a comment body", () => {
    const preview = `${TAGS}\n\nlook here`;
    // 'look here' also appears inside the comment body; the prefix must come
    // from the envelope offset, not a substring search.
    expect(applyQueuedEdit(preview, "look here")).toBe(`${TAGS}\n\nlook here`);
  });

  it("leaves a plain (envelope-free) preview untouched as user text", () => {
    const preview = "just a plain prompt";
    expect(splitPreview(preview).prefix).toBe("");
    expect(splitPreview(preview).userText).toBe(preview);
  });

  it("appends a new queued comment without dropping the existing queued comment", () => {
    const preview = `${TAGS}\n\nfix the bug`;
    const next = applyQueuedInlineTags(preview, [{
      id: "tag-b",
      messageId: "__file__/tmp/b.ts",
      selectedText: "",
      comment: "second comment",
      timestamp: "2026-06-29T00:00:00.000Z",
      fileAnchor: {
        filePath: "/tmp/b.ts",
        startLine: 3,
        endLine: 3,
        startCol: 1,
        endCol: 5,
      },
    }]);
    expect(splitPreview(next).comments.map((comment) => comment.comment)).toEqual([
      "look here",
      "second comment",
    ]);
    expect(splitPreview(next).userText).toBe("fix the bug");
  });

  it("parses comment-only inline tags and preserves the remaining user text", () => {
    const preview = "<inline-tags><comment>Verify card rendering on desktop</comment><comment>Confirm comment cards stay visible and summarized</comment></inline-tags> Remaining user text after the comment tags.";

    const parsed = splitPreview(preview);

    expect(parsed.comments.map((comment) => comment.comment)).toEqual([
      "Verify card rendering on desktop",
      "Confirm comment cards stay visible and summarized",
    ]);
    expect(parsed.userText).toBe("Remaining user text after the comment tags.");
  });
});
