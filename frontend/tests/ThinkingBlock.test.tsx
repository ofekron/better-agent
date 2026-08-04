// setup.ts mocks react-markdown to render the raw source string inside a
// `<div data-test-md>` and ignores the `components` override — so the
// onFileClick click-through (which lives in linkifyFilePaths.tsx) is NOT
// exercisable here and belongs to a future slice. ThinkingBlock's own
// branches (truncation, expand/collapse, indicator, content switch) are
// fully coverable via the rendered source text.
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ThinkingBlock } from "../src/components/ThinkingBlock";

const LONG = "x".repeat(130);
const LONG_PREVIEW = "x".repeat(120) + "...";

function content() {
  return document.querySelector<HTMLElement>("[data-test-md]")!.textContent;
}
function indicator() {
  return screen.getByText(/thinking\.thinking/).textContent;
}

describe("ThinkingBlock", () => {
  it("renders the collapsed indicator and full thought when ≤120 chars", () => {
    const thought = "short thought";
    render(<ThinkingBlock thought={thought} />);
    expect(indicator()).toBe("> thinking.thinking");
    expect(content()).toBe(thought);
  });

  it("truncates the preview to 120 chars + ellipsis when collapsed", () => {
    render(<ThinkingBlock thought={LONG} />);
    expect(content()).toBe(LONG_PREVIEW);
    // The tail beyond 120 is absent from the collapsed preview.
    expect(content()!.length).toBe(LONG_PREVIEW.length);
  });

  it("expands on click: indicator flips to v and shows the full thought", () => {
    render(<ThinkingBlock thought={LONG} />);
    fireEvent.click(screen.getByText(/thinking\.thinking/));
    expect(indicator()).toBe("v thinking.thinking");
    expect(content()).toBe(LONG);
  });

  it("collapses again on a second click", () => {
    const { container } = render(<ThinkingBlock thought={LONG} />);
    const block = container.querySelector(".thinking-block")!;
    fireEvent.click(block);
    expect(indicator()).toBe("v thinking.thinking");
    fireEvent.click(block);
    expect(indicator()).toBe("> thinking.thinking");
    expect(content()).toBe(LONG_PREVIEW);
  });

  it("forwards onFileClick to the markdown linkifier without crashing", () => {
    const onFileClick = vi.fn();
    render(<ThinkingBlock thought="see [a](bcfile:%2Ftmp%2Fa.txt)" onFileClick={onFileClick} />);
    // The linkify components map is built every render with onFileClick;
    // the mock renderer ignores it, but the prop is accepted and forwarded.
    expect(content()).toBe("see [a](bcfile:%2Ftmp%2Fa.txt)");
  });
});
