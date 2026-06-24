import { describe, expect, it } from "vitest";
import { mergeQueuedAttachments } from "../src/utils/sendPromptForm";
import type { FileAttachment, PastedImage } from "../src/types";

const img = (id: string): PastedImage => ({
  dataUrl: `data:image/png;base64,${id}`,
  base64: id,
  mediaType: "image/png",
});
const file = (name: string): FileAttachment => ({
  name,
  base64: name,
  mediaType: "text/plain",
  size: 1,
});

describe("mergeQueuedAttachments", () => {
  it("keeps the previously-queued prompt's image when a new prompt merges in", () => {
    // Regression: queuing a prompt with an image, then merging another
    // prompt used to drop the first prompt's image.
    const merged = mergeQueuedAttachments([img("a")], [], [img("b")], []);
    expect(merged.images.map((i) => i.base64)).toEqual(["a", "b"]);
  });

  it("preserves prior images even when the new send has none", () => {
    const merged = mergeQueuedAttachments([img("a")], [], [], []);
    expect(merged.images.map((i) => i.base64)).toEqual(["a"]);
  });

  it("merges files alongside images, prior first", () => {
    const merged = mergeQueuedAttachments(
      [img("a")],
      [file("old")],
      [img("b")],
      [file("new")],
    );
    expect(merged.images.map((i) => i.base64)).toEqual(["a", "b"]);
    expect(merged.files.map((f) => f.name)).toEqual(["old", "new"]);
  });

  it("returns current arrays untouched when there are no prior attachments", () => {
    const cur = [img("b")];
    const merged = mergeQueuedAttachments(undefined, null, cur, []);
    expect(merged.images).toBe(cur);
    expect(merged.files).toEqual([]);
  });
});
