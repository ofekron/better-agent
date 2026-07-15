import { describe, expect, it, vi } from "vitest";
import { executeComposerSubmission } from "../src/utils/composerSubmission";

describe("composer submission transaction", () => {
  it("commits exactly once after ownership transfers", async () => {
    const begin = vi.fn();
    const commit = vi.fn();
    const rollback = vi.fn();
    await expect(executeComposerSubmission({
      payload: "draft",
      allowed: true,
      submit: () => true,
      begin,
      commit,
      rollback,
    })).resolves.toBe(true);
    expect(begin).toHaveBeenCalledOnce();
    expect(commit).toHaveBeenCalledOnce();
    expect(rollback).not.toHaveBeenCalled();
  });

  it("restores exactly once when ownership is rejected", async () => {
    const begin = vi.fn();
    const commit = vi.fn();
    const rollback = vi.fn();
    await expect(executeComposerSubmission({
      payload: "draft",
      allowed: true,
      submit: () => false,
      begin,
      commit,
      rollback,
    })).resolves.toBe(false);
    expect(begin).toHaveBeenCalledOnce();
    expect(commit).not.toHaveBeenCalled();
    expect(rollback).toHaveBeenCalledOnce();
  });
});
