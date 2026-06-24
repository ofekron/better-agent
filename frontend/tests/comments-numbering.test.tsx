import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyTagHighlights,
  setFocusedTagHighlight,
  PENDING_TAG_ID,
} from "../src/utils/tagHighlights";
import { scrollCommentTargetIntoView } from "../src/utils/commentFocus";
import { CommentsPanel } from "../src/components/CommentsPanel";
import type { InlineTag } from "../src/types/inlineTag";

function tag(overrides: Partial<InlineTag>): InlineTag {
  return {
    id: "t1",
    messageId: "m1",
    selectedText: "",
    comment: "a comment",
    timestamp: "2026-06-12T00:00:00Z",
    ...overrides,
  };
}

function container(html: string): HTMLElement {
  const el = document.createElement("div");
  el.innerHTML = html;
  document.body.appendChild(el);
  return el;
}

afterEach(() => {
  setFocusedTagHighlight(null);
  document.body.innerHTML = "";
});

describe("applyTagHighlights ref markers", () => {
  it("stamps data-ref-number on the LAST span of a numbered tag", () => {
    const el = container("<p>hello <code>brave</code> world</p>");
    applyTagHighlights(el, [
      tag({ id: "t1", selectedText: "hello brave world", displayNumber: 3 }),
    ]);
    const spans = el.querySelectorAll<HTMLElement>(
      '.inline-tag-highlight[data-tag-id="t1"]',
    );
    expect(spans.length).toBeGreaterThan(1);
    const marked = el.querySelectorAll("[data-ref-number]");
    expect(marked.length).toBe(1);
    expect(marked[0]).toBe(spans[spans.length - 1]);
    expect((marked[0] as HTMLElement).dataset.refNumber).toBe("3");
  });

  it("adds no marker without displayNumber (selection preview path)", () => {
    const el = container("<p>some plain text</p>");
    applyTagHighlights(el, [tag({ id: "t1", selectedText: "plain" })]);
    expect(el.querySelectorAll(".inline-tag-highlight").length).toBe(1);
    expect(el.querySelectorAll("[data-ref-number]").length).toBe(0);
  });

  it("does not change the container's text content (no DOM text marker)", () => {
    const el = container("<p>alpha beta gamma</p>");
    applyTagHighlights(el, [
      tag({ id: "t1", selectedText: "beta", displayNumber: 1 }),
    ]);
    expect(el.textContent).toBe("alpha beta gamma");
  });

  it("cleanup removes spans and markers", () => {
    const el = container("<p>alpha beta gamma</p>");
    const cleanup = applyTagHighlights(el, [
      tag({ id: "t1", selectedText: "beta", displayNumber: 1 }),
    ]);
    cleanup();
    expect(el.querySelectorAll(".inline-tag-highlight").length).toBe(0);
    expect(el.querySelectorAll("[data-ref-number]").length).toBe(0);
    expect(el.textContent).toBe("alpha beta gamma");
  });
});

describe("applyTagHighlights selected preview", () => {
  it("emphasizes the live-selection preview (PENDING_TAG_ID) span", () => {
    const el = container("<p>alpha beta gamma</p>");
    applyTagHighlights(el, [tag({ id: PENDING_TAG_ID, selectedText: "beta" })]);
    const selected = el.querySelectorAll(".inline-tag-highlight-selected");
    expect(selected.length).toBe(1);
    expect((selected[0] as HTMLElement).dataset.tagId).toBe(PENDING_TAG_ID);
  });

  it("does not mark a persisted (non-pending) tag as selected", () => {
    const el = container("<p>alpha beta gamma</p>");
    applyTagHighlights(el, [tag({ id: "t1", selectedText: "beta" })]);
    expect(el.querySelectorAll(".inline-tag-highlight-selected").length).toBe(0);
  });
});

describe("setFocusedTagHighlight", () => {
  it("toggles the focused class on the matching tag's spans only", () => {
    const el = container("<p>alpha beta gamma</p>");
    applyTagHighlights(el, [
      tag({ id: "t1", selectedText: "alpha", displayNumber: 1 }),
      tag({ id: "t2", selectedText: "gamma", displayNumber: 2 }),
    ]);
    setFocusedTagHighlight("t2");
    const focused = el.querySelectorAll(".inline-tag-highlight-focused");
    expect(focused.length).toBe(1);
    expect((focused[0] as HTMLElement).dataset.tagId).toBe("t2");

    setFocusedTagHighlight("t1");
    const refocused = el.querySelectorAll<HTMLElement>(
      ".inline-tag-highlight-focused",
    );
    expect(refocused.length).toBe(1);
    expect(refocused[0].dataset.tagId).toBe("t1");

    setFocusedTagHighlight(null);
    expect(el.querySelectorAll(".inline-tag-highlight-focused").length).toBe(0);
  });

  it("spans created AFTER focus come up already focused (re-render path)", () => {
    setFocusedTagHighlight("t1");
    const el = container("<p>alpha beta gamma</p>");
    applyTagHighlights(el, [
      tag({ id: "t1", selectedText: "beta", displayNumber: 1 }),
    ]);
    expect(el.querySelectorAll(".inline-tag-highlight-focused").length).toBe(1);
  });
});

describe("CommentsPanel numbering", () => {
  it("renders each card's displayNumber badge", () => {
    render(
      <CommentsPanel
        tags={[
          tag({ id: "t1", comment: "first", displayNumber: 1 }),
          tag({ id: "t2", comment: "second", displayNumber: 2 }),
        ]}
        onRemove={() => {}}
        onUpdate={() => {}}
        focusedCommentId={null}
        onFocusComment={() => {}}
      />,
    );
    const badges = Array.from(
      document.querySelectorAll(".comments-panel-card-number"),
    ).map((b) => b.textContent);
    expect(badges).toEqual(["1", "2"]);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByText("second")).toBeTruthy();
  });

  it("auto-edits a new comment without focusing it and scrolling the chat", async () => {
    const onFocusComment = vi.fn();
    const onAutoEditConsumed = vi.fn();
    render(
      <CommentsPanel
        tags={[tag({ id: "t1", comment: "", displayNumber: 1 })]}
        onRemove={() => {}}
        onUpdate={() => {}}
        focusedCommentId={null}
        onFocusComment={onFocusComment}
        autoEditId="t1"
        onAutoEditConsumed={onAutoEditConsumed}
      />,
    );

    await waitFor(() => expect(onAutoEditConsumed).toHaveBeenCalledOnce());
    expect(document.querySelector(".comments-panel-card-textarea")).toBeTruthy();
    expect(onFocusComment).not.toHaveBeenCalled();
  });
});

describe("scrollCommentTargetIntoView", () => {
  it("scrolls to the start of the highlighted text without selecting it", () => {
    const scrollEl = container(
      '<div class="chat-messages"><span class="inline-tag-highlight" data-tag-id="t1">second</span><span class="inline-tag-highlight" data-tag-id="t1">first</span></div>',
    ).firstElementChild as HTMLElement;
    let scrollTop = 30;
    let scrollArg: ScrollToOptions | undefined;
    Object.defineProperty(scrollEl, "scrollTop", {
      get: () => scrollTop,
      set: (value) => {
        scrollTop = Number(value);
      },
      configurable: true,
    });
    scrollEl.scrollTo = (arg?: ScrollToOptions | number) => {
      if (typeof arg === "object") scrollArg = arg;
    };
    scrollEl.getBoundingClientRect = () => ({
      top: 100,
      bottom: 300,
      left: 0,
      right: 500,
      width: 500,
      height: 200,
      x: 0,
      y: 100,
      toJSON: () => ({}),
    });
    const spans = scrollEl.querySelectorAll<HTMLElement>(".inline-tag-highlight");
    spans[0].getBoundingClientRect = () => ({
      top: 180,
      bottom: 200,
      left: 0,
      right: 50,
      width: 50,
      height: 20,
      x: 0,
      y: 180,
      toJSON: () => ({}),
    });
    spans[1].getBoundingClientRect = () => ({
      top: 150,
      bottom: 170,
      left: 0,
      right: 50,
      width: 50,
      height: 20,
      x: 0,
      y: 150,
      toJSON: () => ({}),
    });
    const selection = window.getSelection();
    selection?.removeAllRanges();

    scrollCommentTargetIntoView("t1", [tag({ id: "t1", selectedText: "first second" })]);

    expect(scrollArg).toEqual({ top: 56, behavior: "smooth" });
    expect(window.getSelection()?.rangeCount).toBe(0);
  });
});
