import { describe, expect, it } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { MessageBox } from "../src/components/MessageBubble";

// MessageBox is a memoized presentational wrapper around ScaledMarkdown with a
// collapsible state machine. In production it is only reached via OutputEvent's
// prose path, which always passes defaultOpen=true (collapsible defaults true),
// so the collapsed-preview branch, the close side of the toggle, and the
// open=false aria/arrow branches are unreachable through that caller. This
// slice exercises MessageBox directly to lock every branch of its own state:
// the collapsed preview render, the toggle close, the toggle open, and the
// collapsed-body click-to-expand.

const has = (c: HTMLElement, sel: string) => expect(c.querySelector(sel)).not.toBeNull();
const missing = (c: HTMLElement, sel: string) => expect(c.querySelector(sel)).toBeNull();

describe("MessageBox collapsed preview", () => {
  it("renders the collapsed preview instead of the body when defaultOpen=false", () => {
    const { container } = render(
      <MessageBox text={"First line preview\nsecond line"} defaultOpen={false} />,
    );
    has(container, ".message-box-collapsed-body");
    missing(container, ".message-box-body");
    // firstLineSummary returns the first non-empty trimmed line.
    expect(container.querySelector(".message-box-collapsed-body")?.textContent).toBe(
      "First line preview",
    );
    // Closed-state chrome: arrow points right, toggle reports not expanded.
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    expect(container.querySelector(".message-box-toggle")?.getAttribute("aria-expanded")).toBe(
      "false",
    );
    cleanup();
  });

  it("expands into the body when the collapsed preview is clicked", () => {
    const { container } = render(
      <MessageBox text={"Preview line\nsecond"} defaultOpen={false} />,
    );
    missing(container, ".message-box-body");
    fireEvent.click(container.querySelector(".message-box-collapsed-body")!);
    has(container, ".message-box-body");
    missing(container, ".message-box-collapsed-body");
    // Toggling open flips the arrow and aria-expanded.
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▼");
    expect(container.querySelector(".message-box-toggle")?.getAttribute("aria-expanded")).toBe(
      "true",
    );
    cleanup();
  });
});

describe("MessageBox toggle button", () => {
  it("collapses an open body via the toggle and reflects closed-state chrome", () => {
    const { container } = render(<MessageBox text={"Open body text"} defaultOpen={true} />);
    has(container, ".message-box-body");
    expect(container.querySelector(".message-box-toggle")?.getAttribute("aria-expanded")).toBe(
      "true",
    );
    fireEvent.click(container.querySelector(".message-box-toggle")!);
    missing(container, ".message-box-body");
    has(container, ".message-box-collapsed-body");
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    expect(container.querySelector(".message-box-toggle")?.getAttribute("aria-expanded")).toBe(
      "false",
    );
    cleanup();
  });

  it("re-expands a collapsed box via the toggle button", () => {
    const { container } = render(<MessageBox text={"Some text"} defaultOpen={false} />);
    missing(container, ".message-box-body");
    fireEvent.click(container.querySelector(".message-box-toggle")!);
    has(container, ".message-box-body");
    missing(container, ".message-box-collapsed-body");
    cleanup();
  });
});
