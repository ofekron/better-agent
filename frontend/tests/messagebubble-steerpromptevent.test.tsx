import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import type { ChatMessage } from "../src/types";
import { SteerPromptEvent } from "../src/components/MessageBubble";

// SteerPromptEvent renders a user "steer" prompt. Branch coverage targets:
// the hasArtificialSections ternary — linkifyFilePaths (plain prompt) vs
// UserContentSegments/ArtificialSectionChip (prompt wrapped in an allowed
// artificial tag) — and UserImages (renders attached images vs null when none).

describe("SteerPromptEvent shell", () => {
  it("renders the Steer label and text container", () => {
    const { container } = render(<SteerPromptEvent prompt="hello" />);
    expect(container.querySelector(".event-steer-prompt")).not.toBeNull();
    expect(container.querySelector(".event-steer-label")?.textContent).toBe("Steer");
    expect(container.querySelector(".event-steer-text")).not.toBeNull();
  });
});

describe("SteerPromptEvent plain prompt (non-artificial)", () => {
  it("renders the prompt text inline without an artificial-section chip", () => {
    const { container } = render(<SteerPromptEvent prompt="push the button" />);
    expect(container.querySelector(".event-steer-text")?.textContent).toContain("push the button");
    expect(container.querySelector(".artificial-section-chip")).toBeNull();
  });

  it("renders nothing for the text body when the prompt is empty", () => {
    const { container } = render(<SteerPromptEvent prompt="" />);
    // Empty prompt has no artificial sections and linkifyFilePaths("") yields
    // no text node; only the static label remains.
    expect(container.querySelector(".artificial-section-chip")).toBeNull();
    expect(container.querySelector(".event-steer-text")?.textContent).toBe("");
  });
});

describe("SteerPromptEvent artificial prompt", () => {
  it("collapses an allowed tag into an ArtificialSectionChip", () => {
    const prompt = "<system-reminder>be brief</system-reminder>";
    const { container } = render(<SteerPromptEvent prompt={prompt} />);
    const chip = container.querySelector(".artificial-section-chip");
    expect(chip).not.toBeNull();
    // system-reminder -> "System reminder" label, rendered in the chip header.
    expect(chip?.querySelector(".artificial-section-label")?.textContent).toBe("System reminder");
    // Collapsed by default (caret "▸"): no expanded body, inner text shown only as preview.
    expect(chip?.querySelector(".artificial-section-body")).toBeNull();
    expect(chip?.querySelector(".artificial-section-preview")?.textContent).toContain("be brief");
  });

  it("unwraps a user_prompt tag as inline real text, not a chip", () => {
    const prompt = "<user_prompt>real steer text</user_prompt>";
    const { container } = render(<SteerPromptEvent prompt={prompt} />);
    expect(container.querySelector(".artificial-section-chip")).toBeNull();
    expect(container.querySelector(".event-steer-text")?.textContent).toContain("real steer text");
  });
});

describe("SteerPromptEvent images", () => {
  it("renders no image gallery when images are absent", () => {
    const { container } = render(<SteerPromptEvent prompt="hello" />);
    expect(container.querySelector(".message-images")).toBeNull();
  });

  it("renders the image gallery when a dataUrl image is attached", () => {
    const images: ChatMessage["images"] = [
      { media_type: "image/png", dataUrl: "data:image/png;base64,AAAA" },
    ];
    const { container } = render(<SteerPromptEvent prompt="see this" images={images} sessionId="s1" />);
    const gallery = container.querySelector(".message-images");
    expect(gallery).not.toBeNull();
    const img = gallery?.querySelector("img.message-image") as HTMLImageElement | null;
    expect(img?.getAttribute("src")).toBe("data:image/png;base64,AAAA");
  });
});
