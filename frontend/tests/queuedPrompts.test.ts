import { describe, expect, it } from "vitest";
import {
  queuedPromptToVisibleBanner,
  visibleQueuedPromptBanners,
} from "../src/utils/queuedPrompts";
import type { QueuedPrompt } from "../src/types";

const queuedPrompt = (
  id: string,
  kind: QueuedPrompt["kind"],
): QueuedPrompt => ({
  id,
  lifecycle_msg_id: `life-${id}`,
  content: `prompt ${id}`,
  kind,
  queue_position: 0,
  images_count: 1,
  files_count: 2,
});

describe("queued prompt visibility", () => {
  it("hides backend internal send queue rows from user-visible banners", () => {
    expect(queuedPromptToVisibleBanner(queuedPrompt("send-1", "send"))).toBeNull();
    expect(queuedPromptToVisibleBanner(queuedPrompt("unknown-1", undefined))).toBeNull();
  });

  it("keeps only backend-confirmed user-visible queue rows", () => {
    expect(visibleQueuedPromptBanners([
      queuedPrompt("send-1", "send"),
      queuedPrompt("queued-1", "queued_behind"),
      queuedPrompt("interrupt-1", "interrupt"),
    ])).toEqual([
      {
        id: "queued-1",
        preview: "prompt queued-1",
        imagesCount: 1,
        filesCount: 2,
      },
      {
        id: "interrupt-1",
        preview: "prompt interrupt-1",
        imagesCount: 1,
        filesCount: 2,
      },
    ]);
  });
});
