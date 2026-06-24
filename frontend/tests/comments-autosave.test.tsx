import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommentsPanel } from "../src/components/CommentsPanel";
import type { InlineTag } from "../src/types/inlineTag";

function tag(overrides: Partial<InlineTag>): InlineTag {
  return {
    id: "t1",
    messageId: "m1",
    selectedText: "",
    comment: "original",
    timestamp: "2026-06-12T00:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.useRealTimers();
});

function enterEdit() {
  fireEvent.click(screen.getByLabelText("Edit comment"));
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

describe("CommentsPanel always-saves edits", () => {
  it("has no Save / Cancel buttons in edit mode", () => {
    render(
      <CommentsPanel
        tags={[tag({})]}
        onRemove={vi.fn()}
        onUpdate={vi.fn()}
        focusedCommentId={null}
        onFocusComment={vi.fn()}
      />,
    );
    enterEdit();
    expect(screen.queryByText("Save")).toBeNull();
    expect(screen.queryByText("Cancel")).toBeNull();
  });

  it("persists edits after the debounce without any button press", async () => {
    const onUpdate = vi.fn();
    render(
      <CommentsPanel
        tags={[tag({})]}
        onRemove={vi.fn()}
        onUpdate={onUpdate}
        focusedCommentId={null}
        onFocusComment={vi.fn()}
      />,
    );
    const ta = enterEdit();
    fireEvent.change(ta, { target: { value: "edited live" } });
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledWith("t1", { comment: "edited live" }),
    );
  });

  it("flushes the save immediately on blur", () => {
    const onUpdate = vi.fn();
    render(
      <CommentsPanel
        tags={[tag({})]}
        onRemove={vi.fn()}
        onUpdate={onUpdate}
        focusedCommentId={null}
        onFocusComment={vi.fn()}
      />,
    );
    const ta = enterEdit();
    fireEvent.change(ta, { target: { value: "blur saves" } });
    fireEvent.blur(ta);
    expect(onUpdate).toHaveBeenCalledWith("t1", { comment: "blur saves" });
    // Editor closed.
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("removes the comment when cleared to empty", () => {
    const onRemove = vi.fn();
    const onUpdate = vi.fn();
    render(
      <CommentsPanel
        tags={[tag({})]}
        onRemove={onRemove}
        onUpdate={onUpdate}
        focusedCommentId={null}
        onFocusComment={vi.fn()}
      />,
    );
    const ta = enterEdit();
    fireEvent.change(ta, { target: { value: "   " } });
    fireEvent.blur(ta);
    expect(onRemove).toHaveBeenCalledWith("t1");
    expect(onUpdate).not.toHaveBeenCalled();
  });
});
