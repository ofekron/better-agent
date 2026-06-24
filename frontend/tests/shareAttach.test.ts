import { describe, it, expect } from "vitest";
import { buildShareDraftPatch } from "../src/utils/shareAttach";
import type { PastedImage, Session } from "../src/types";

const img = (id: string): PastedImage => ({
  dataUrl: `data:image/jpeg;base64,${id}`,
  base64: id,
  mediaType: "image/jpeg",
});

const session = (): Session =>
  ({ id: "s1", name: "S", cwd: "/p", draft_input: "", messages: [] } as unknown as Session);

describe("buildShareDraftPatch", () => {
  it("merges shared images AFTER existing draft_images (no overwrite)", () => {
    const target = { ...session(), draft_images: [img("old")] } as Session;
    const patch = buildShareDraftPatch(target, [img("new1"), img("new2")]);
    expect(patch.draft_images.map((i) => i.base64)).toEqual(["old", "new1", "new2"]);
  });

  it("preserves the target session's own draft_input", () => {
    const target = { ...session(), draft_input: "keep me", draft_images: [] } as Session;
    const patch = buildShareDraftPatch(target, [img("a")]);
    expect(patch.draft_input).toBe("keep me");
  });

  it("handles a target with no existing draft fields", () => {
    const target = { ...session() } as Session;
    delete (target as Partial<Session>).draft_images;
    const patch = buildShareDraftPatch(target, [img("a"), img("b")]);
    expect(patch.draft_input).toBe("");
    expect(patch.draft_images.map((i) => i.base64)).toEqual(["a", "b"]);
  });

  it("supports multiple shared images (SEND_MULTIPLE)", () => {
    const patch = buildShareDraftPatch(undefined, [img("a"), img("b"), img("c")]);
    expect(patch.draft_images).toHaveLength(3);
  });
});
