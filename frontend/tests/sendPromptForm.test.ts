import { describe, expect, it } from "vitest";
import { buildSendPromptForm } from "../src/utils/sendPromptForm";

describe("buildSendPromptForm", () => {
  it("uses the merged queued prompt as the single prompt form", () => {
    const form = buildSendPromptForm({
      finalPrompt: "continue why did uy stop?",
      sendMode: "queue",
      existingQueuedPreview:
        "8ecfdc15-ce00-4f68-a370-88036f78a9ed b9b1a7f7-393f-438d-abb3-b98999c96f11 /workspace/testape this mssg show stale data on ui",
    });

    expect(form).toEqual({
      prompt:
        "8ecfdc15-ce00-4f68-a370-88036f78a9ed b9b1a7f7-393f-438d-abb3-b98999c96f11 /workspace/testape this mssg show stale data on ui\n\n---\n\ncontinue why did uy stop?",
      replacedQueuedPrompt: true,
    });
  });

  it("does not merge non-queue sends", () => {
    expect(
      buildSendPromptForm({
        finalPrompt: "interrupt now",
        sendMode: "interrupt",
        existingQueuedPreview: "queued text",
      }),
    ).toEqual({
      prompt: "interrupt now",
      replacedQueuedPrompt: false,
    });
  });
});
