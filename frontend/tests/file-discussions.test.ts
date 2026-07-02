import { describe, expect, it } from "vitest";
import {
  isValidEmptyFileEditSession,
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

  it("only treats explicit empty file-edit metadata as an empty file-edit session", () => {
    expect(isValidEmptyFileEditSession({
      id: "empty",
      name: "empty",
      cwd: "/tmp",
      model: "test",
      messages: [],
      working_mode: "file_editing",
      working_mode_meta: {
        file_paths: [],
        original_contents: {},
      },
    })).toBe(true);

    expect(isValidEmptyFileEditSession({
      id: "legacy",
      name: "legacy",
      cwd: "/tmp",
      model: "test",
      messages: [],
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/a.ts",
      },
    })).toBe(false);

    expect(isValidEmptyFileEditSession({
      id: "normal",
      name: "normal",
      cwd: "/tmp",
      model: "test",
      messages: [],
      working_mode_meta: {
        file_paths: [],
      },
    })).toBe(false);
  });
});
