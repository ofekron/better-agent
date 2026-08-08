// `ArtificialSectionSegments` (src/surface/leaf/ArtificialSections.tsx) —
// the single shared native renderer for artificial-section text (used by
// both TypedPromptView and SteeringMessageView; see round 2's item 5,
// "single parser, no fork"). Direct component-level coverage of the
// generic chip/expand/inline-tags/unwrap machinery itself, at parity with
// legacy MessageBubble.tsx's `UserContentSegments`/`ArtificialSectionChip`.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ArtificialSectionSegments } from "../../src/surface/leaf/ArtificialSections";
import { buildInlineTagsPreamble } from "../../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../../src/types/inlineTag";

describe("ArtificialSectionSegments", () => {
  it("plain text with no tags renders as markdown, untouched", () => {
    render(<ArtificialSectionSegments text="just a normal prompt" />);
    expect(screen.getByText("just a normal prompt")).toBeTruthy();
  });

  it("a known tag renders as a collapsed chip, not raw XML", () => {
    const { container } = render(
      <ArtificialSectionSegments text="before <system-reminder>be brief</system-reminder> after" />,
    );
    expect(container.textContent).not.toContain("<system-reminder>");
    expect(container.querySelector(".artificial-section-body")).toBeNull();
    const chip = container.querySelector(".artificial-section-chip.artificial-section-system-reminder");
    expect(chip).not.toBeNull();
    expect(chip?.querySelector(".artificial-section-label")?.textContent).toBe("System reminder");
    expect(chip?.querySelector(".artificial-section-preview")?.textContent).toContain("be brief");
    expect(container.textContent).toContain("before");
    expect(container.textContent).toContain("after");
  });

  it("clicking a chip header expands it to reveal the body", () => {
    const { container } = render(<ArtificialSectionSegments text="<system-reminder>be brief</system-reminder>" />);
    const button = container.querySelector(".artificial-section-header") as HTMLButtonElement;
    fireEvent.click(button);
    expect(container.querySelector(".artificial-section-body")?.textContent).toContain("be brief");
  });

  it("surfaces a path attribute as a hint on the collapsed header", () => {
    const { container } = render(
      <ArtificialSectionSegments text='<file-comment path="src/app.ts">looks good</file-comment>' />,
    );
    expect(container.querySelector(".artificial-section-hint")?.textContent).toBe("src/app.ts");
  });

  it("the unwrap tag (user_prompt) renders its inner text inline, no chip", () => {
    const { container } = render(<ArtificialSectionSegments text="<user_prompt>my real question</user_prompt>" />);
    expect(container.querySelector(".artificial-section-chip")).toBeNull();
    expect(container.textContent).toContain("my real question");
  });

  it("inline-tags renders as comment cards, auto-expanded", () => {
    const tag: InlineTag = {
      id: "t1",
      messageId: "u1",
      selectedText: "selected code",
      comment: "tighten this",
      timestamp: "2026-06-15T10:00:00.000Z",
    };
    const preamble = buildInlineTagsPreamble([tag]);
    render(<ArtificialSectionSegments text={`${preamble}\nApply the note.`} />);
    // Auto-expanded (no click needed) — the comment-card content is visible.
    expect(screen.getByText("tighten this")).toBeTruthy();
    expect(screen.getByText("selected code")).toBeTruthy();
    const card = document.querySelector(".comment-card.inline-tags-card");
    expect(card).not.toBeNull();
    expect(screen.getByText(/Apply the note\./)).toBeTruthy();
  });
});
