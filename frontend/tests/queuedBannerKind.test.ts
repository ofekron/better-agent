import { describe, expect, it } from "vitest";
import {
  isBannerQueuedKind,
  queuedPromptToVisibleBanner,
} from "../src/utils/queuedPrompts";
import type { QueuedPrompt } from "../src/types";

// The user_message_queued lifecycle handler clears the optimistic
// "Sending…" bubble for banner-worthy kinds (the queue banner replaces it).
// The original bug only checked "queued_behind", so an interrupt-queued
// prompt showed BOTH the bubble and the banner. isBannerQueuedKind is now
// the single source of truth for that decision, backed by the same set the
// banner projection uses.

describe("isBannerQueuedKind", () => {
  it("treats interrupt and queued_behind as banner-worthy (clear the Sending bubble)", () => {
    expect(isBannerQueuedKind("queued_behind")).toBe(true);
    expect(isBannerQueuedKind("interrupt")).toBe(true);
  });

  it("treats send and a missing kind as not banner-worthy (keep the Sending bubble)", () => {
    expect(isBannerQueuedKind("send")).toBe(false);
    expect(isBannerQueuedKind(undefined)).toBe(false);
  });

  it("rejects unknown kinds — fail-safe keeps the bubble rather than dropping it", () => {
    expect(isBannerQueuedKind("nonsense")).toBe(false);
    expect(isBannerQueuedKind("")).toBe(false);
  });
});

describe("queuedPromptToVisibleBanner uses the same predicate", () => {
  function prompt(kind: QueuedPrompt["kind"]): QueuedPrompt {
    return {
      id: "q1",
      lifecycle_msg_id: "life-q1",
      content: "hi",
      kind,
      queue_position: 0,
      images_count: 0,
      files_count: 0,
    };
  }

  it("renders a banner for banner-worthy kinds only", () => {
    expect(queuedPromptToVisibleBanner(prompt("queued_behind"))).not.toBeNull();
    expect(queuedPromptToVisibleBanner(prompt("interrupt"))).not.toBeNull();
    expect(queuedPromptToVisibleBanner(prompt("send"))).toBeNull();
  });
});
