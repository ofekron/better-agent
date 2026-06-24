import { describe, expect, it } from "vitest";
import {
  patchFileDiscussionMeta,
  upsertFileDiscussionMeta,
} from "../src/utils/fileDiscussions";

describe("file discussion metadata helpers", () => {
  it("upserts the discussion returned by the start endpoint into working_mode_meta", () => {
    const meta = upsertFileDiscussionMeta(
      {
        file_paths: ["/tmp/a.ts"],
        original_contents: {},
      },
      {
        id: "fd_1",
        file_path: "/tmp/a.ts",
        line: 4,
      },
    );

    expect(meta.file_discussions).toEqual([
      {
        id: "fd_1",
        file_path: "/tmp/a.ts",
        line: 4,
      },
    ]);
  });

  it("patches the discussion returned by the collapse endpoint", () => {
    const meta = patchFileDiscussionMeta(
      {
        file_discussions: [
          {
            id: "fd_1",
            file_path: "/tmp/a.ts",
            line: 4,
            collapsed: false,
          },
        ],
      },
      "fd_1",
      {
        id: "fd_1",
        file_path: "/tmp/a.ts",
        line: 4,
        collapsed: true,
      },
    );

    expect(meta.file_discussions?.[0].collapsed).toBe(true);
  });
});
