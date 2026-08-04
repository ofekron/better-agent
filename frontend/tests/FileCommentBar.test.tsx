import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import i18n from "i18next";
import { clearError } from "../src/progress/store";

import {
  FileCommentBar,
  type CommentSelection,
  type SubmittedComment,
} from "../src/components/FileCommentBar";

/**
 * FileCommentBar unit slice. i18n runs REAL (via setup.ts i18n instance); we
 * add the `fileComment.*` resource bundle here so {{selection}} / {{count}}
 * interpolation is exercised honestly — assertions check the formatted
 * selection/count, not bare keys. The progress store (trackPromise /
 * useOpProgress) and usePersistedDraft (localStorage) are real, in-memory,
 * deterministic — no mocks. window.alert is stubbed because happy-dom has no
 * native alert. The "return focus to prompt bar" target lives OUTSIDE this
 * component, so we mount those elements on document.body directly.
 */

const OP_ID = "comment:submit";

const FILE_COMMENT_EN: Record<string, string> = {
  "fileComment.commentOn": "Comment on {{selection}}",
  "fileComment.placeholder": "What should change here? (Cmd/Ctrl+Enter to submit)",
  "fileComment.queueing": "Queueing…",
  "fileComment.queueComment": "Queue comment",
  "fileComment.queuedHint":
    "{{count}} comment(s) queued — keep selecting to add more.",
  "fileComment.emptyHint": "Select text in the file to queue a comment.",
  "fileComment.failedToAdd": "Failed to add comment: ",
};

function loadI18n(): void {
  i18n.addResourceBundle("en", "translation", FILE_COMMENT_EN, true, true);
}

/** Manual deferred so a test can hold onSubmit inflight, then settle it. */
function deferred<T = void>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Append a focus-target element to document.body (the "main prompt bar"). */
function mountPromptTarget(
  attr: "data-testid" | "class",
  value: string,
): HTMLTextAreaElement & { focus: ReturnType<typeof vi.fn> } {
  const el = document.createElement("textarea") as HTMLTextAreaElement & {
    focus: ReturnType<typeof vi.fn>;
  };
  el.focus = vi.fn();
  if (attr === "data-testid") el.setAttribute("data-testid", value);
  else el.className = value;
  document.body.appendChild(el);
  return el;
}

function bar(props: {
  selection?: CommentSelection | null;
  onSubmit?: (c: SubmittedComment) => Promise<void>;
  onCancel?: () => void;
  pendingTagCount?: number;
  draftKey?: string | null;
}) {
  const onSubmit =
    props.onSubmit ??
    (async () => {
      /* no-op success by default */
    });
  const onCancel = props.onCancel ?? (() => {});
  return render(
    <FileCommentBar
      selection={props.selection ?? null}
      onSubmit={onSubmit}
      onCancel={onCancel}
      pendingTagCount={props.pendingTagCount}
      draftKey={props.draftKey}
    />,
  );
}

/** The "Queue comment" submit button, found by its i18n label. */
function getQueueBtn(container: HTMLElement): HTMLButtonElement {
  return Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === "Queue comment",
  )!;
}

/** The comment textarea. */
function getCommentInput(container: HTMLElement): HTMLTextAreaElement {
  return container.querySelector(".file-comment-input") as HTMLTextAreaElement;
}

const MONO_ONE = {
  kind: "monaco" as const,
  startLine: 1,
  endLine: 1,
  startCol: 1,
  endCol: 2,
};

const alertMock = vi.fn();

beforeEach(() => {
  loadI18n();
  alertMock.mockClear();
  // happy-dom ships no window.alert; the component calls the bare global.
  vi.stubGlobal("alert", alertMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearError(OP_ID);
  document
    .querySelectorAll("[data-testid='input-textarea'], textarea.input-prompt")
    .forEach((el) => el.remove());
});

describe("FileCommentBar — empty state", () => {
  it("shows the empty hint when no selection and no pending tags", () => {
    const { container } = bar({});
    const hint = container.querySelector(".file-comment-bar-hint")!;
    expect(hint.textContent).toBe(
      FILE_COMMENT_EN["fileComment.emptyHint"],
    );
  });

  it("shows the queued hint with the interpolated count when pendingTagCount > 0", () => {
    const { container } = bar({ pendingTagCount: 3 });
    const hint = container.querySelector(".file-comment-bar-hint")!;
    expect(hint.textContent).toContain("3 comment(s) queued");
  });
});

describe("FileCommentBar — describeSelection rendering", () => {
  it("formats a single-line monaco selection as line + col range", () => {
    const { container } = bar({
      selection: {
        kind: "monaco",
        startLine: 7,
        endLine: 7,
        startCol: 3,
        endCol: 12,
      },
    });
    expect(container.querySelector(".file-comment-bar-anchor")!.textContent).toBe(
      "Comment on line 7 (col 3–12)",
    );
  });

  it("formats a multi-line monaco selection as a start → end range", () => {
    const { container } = bar({
      selection: {
        kind: "monaco",
        startLine: 4,
        endLine: 9,
        startCol: 2,
        endCol: 18,
      },
    });
    expect(container.querySelector(".file-comment-bar-anchor")!.textContent).toBe(
      "Comment on 4:2 → 9:18",
    );
  });

  it("quotes short text selections verbatim", () => {
    const { container } = bar({
      selection: { kind: "text", selectedText: "fix me" },
    });
    expect(container.querySelector(".file-comment-bar-anchor")!.textContent).toBe(
      'Comment on “fix me”',
    );
  });

  it("truncates and ellipsizes text selections longer than 60 chars", () => {
    const long = "x".repeat(80);
    const { container } = bar({
      selection: { kind: "text", selectedText: long },
    });
    const anchor = container.querySelector(".file-comment-bar-anchor")!;
    expect(anchor.textContent).toContain("…");
    expect(anchor.textContent).not.toContain("x".repeat(80));
    // first 60 chars preserved before the ellipsis
    expect(anchor.textContent).toContain("x".repeat(60));
  });

  it("collapses internal/edge whitespace in text selections", () => {
    const { container } = bar({
      selection: { kind: "text", selectedText: "  lead\n\n\tgap   tail  " },
    });
    expect(container.querySelector(".file-comment-bar-anchor")!.textContent).toBe(
      'Comment on “lead gap tail”',
    );
  });
});

describe("FileCommentBar — drafting + queue submit", () => {
  it("submits the trimmed comment via the Queue button and clears the field on success", async () => {
    const submitted: { selection: unknown; comment: string }[] = [];
    const { container } = bar({
      selection: MONO_ONE,
      onSubmit: async (c) => {
        submitted.push(c);
      },
    });
    const target = mountPromptTarget("data-testid", "input-textarea");
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);

    fireEvent.change(textarea, { target: { value: "  hello world  " } });
    expect(textarea.value).toBe("  hello world  ");

    fireEvent.click(btn);

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0].comment).toBe("hello world"); // trimmed
    expect(submitted[0].selection).toMatchObject({ startLine: 1 });
    await waitFor(() => expect(textarea.value).toBe("")); // cleared
    expect(target.focus).toHaveBeenCalled(); // focus returned to prompt bar
  });

  it("trims and rejects empty/whitespace-only comments at the guard", () => {
    const onSubmit = vi.fn(async () => {});
    const { container } = bar({
      selection: MONO_ONE,
      onSubmit,
    });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);
    // Queue button is business-disabled while comment is blank.
    expect(btn.disabled).toBe(true);

    fireEvent.change(textarea, { target: { value: "    " } });
    fireEvent.click(btn);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("prevents a second concurrent submit while one is inflight", async () => {
    const gate = deferred<void>();
    const onSubmit = vi.fn(() => gate.promise);
    const { container } = bar({ selection: MONO_ONE, onSubmit });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);

    fireEvent.change(textarea, { target: { value: "first" } });
    fireEvent.click(btn);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));

    // While inflight the textarea disables and a second submit is a no-op.
    await waitFor(() => expect(textarea.disabled).toBe(true));
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);

    gate.resolve(undefined);
    await waitFor(() => expect(textarea.disabled).toBe(false));
    await waitFor(() => expect(textarea.value).toBe(""));
  });

  it("alerts with the error message when onSubmit rejects with an Error", async () => {
    const { container } = bar({
      selection: MONO_ONE,
      onSubmit: async () => {
        throw new Error("boom");
      },
    });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);

    fireEvent.change(textarea, { target: { value: "c" } });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(alertMock).toHaveBeenCalledWith("Failed to add comment: boom"),
    );
    expect(textarea.value).toBe("c"); // NOT cleared on failure
  });

  it("alerts with String(e) when onSubmit rejects with a non-Error", async () => {
    const { container } = bar({
      selection: MONO_ONE,
      onSubmit: async () => {
        throw "stranger"; // eslint-disable-line no-throw-literal
      },
    });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);

    fireEvent.change(textarea, { target: { value: "c" } });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(alertMock).toHaveBeenCalledWith(
        "Failed to add comment: stranger",
      ),
    );
  });
});

describe("FileCommentBar — focus-target fallback chain", () => {
  it("focuses textarea.input-prompt when the data-testid target is absent", async () => {
    const fallback = mountPromptTarget("class", "input-prompt");
    const { container } = bar({ selection: MONO_ONE, onSubmit: async () => {} });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);
    fireEvent.change(textarea, { target: { value: "x" } });
    fireEvent.click(btn);
    await waitFor(() => expect(fallback.focus).toHaveBeenCalled());
  });

  it("does not throw when neither focus target exists", async () => {
    const { container } = bar({ selection: MONO_ONE, onSubmit: async () => {} });
    const textarea = getCommentInput(container);
    const btn = getQueueBtn(container);
    fireEvent.change(textarea, { target: { value: "x" } });
    fireEvent.click(btn);
    await waitFor(() => expect(textarea.value).toBe(""));
  });
});

describe("FileCommentBar — keyboard shortcuts", () => {
  function setup() {
    const onSubmit = vi.fn(async () => {});
    const r = bar({ selection: MONO_ONE, onSubmit });
    const textarea = getCommentInput(r.container);
    fireEvent.change(textarea, { target: { value: "go" } });
    return { onSubmit, textarea };
  }

  it("submits on Ctrl+Enter and prevents default", () => {
    const { onSubmit, textarea } = setup();
    // fireEvent returns false when a cancelable event's preventDefault ran.
    const dispatched = fireEvent.keyDown(textarea, {
      key: "Enter",
      ctrlKey: true,
    });
    expect(dispatched).toBe(false);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("submits on Cmd+Enter (metaKey short-circuits the modifier ||)", () => {
    const { onSubmit, textarea } = setup();
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does not submit on plain Enter (no modifier) and leaves default intact", () => {
    const { onSubmit, textarea } = setup();
    const dispatched = fireEvent.keyDown(textarea, { key: "Enter" });
    expect(dispatched).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not submit on Ctrl+<non-Enter> (key !== Enter)", () => {
    const { onSubmit, textarea } = setup();
    const dispatched = fireEvent.keyDown(textarea, {
      key: "s",
      ctrlKey: true,
    });
    expect(dispatched).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not submit on Ctrl+Enter when the comment is whitespace-only", () => {
    const onSubmit = vi.fn(async () => {});
    const { container } = bar({ selection: MONO_ONE, onSubmit });
    const textarea = getCommentInput(container);
    fireEvent.change(textarea, { target: { value: "   " } });
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("FileCommentBar — cancel", () => {
  it("clears the draft and calls onCancel", () => {
    const onCancel = vi.fn();
    const { container } = bar({ selection: MONO_ONE, onCancel });
    const textarea = getCommentInput(container);
    const cancelBtn = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Cancel",
    )!;
    fireEvent.change(textarea, { target: { value: "draft" } });
    expect(textarea.value).toBe("draft");

    fireEvent.click(cancelBtn);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(textarea.value).toBe("");
  });
});

describe("FileCommentBar — persisted draft (draftKey)", () => {
  it("persists the in-progress draft to localStorage and restores it on remount", () => {
    const { container, unmount } = bar({
      selection: MONO_ONE,
      draftKey: "src/app.ts",
    });
    const textarea = getCommentInput(container);
    fireEvent.change(textarea, { target: { value: "remember me" } });
    expect(localStorage.getItem("comment-draft:src/app.ts")).toBe(
      "remember me",
    );

    unmount();
    const r2 = bar({ selection: MONO_ONE, draftKey: "src/app.ts" });
    expect(getCommentInput(r2.container).value).toBe("remember me");
  });

  it("uses the null key (in-memory only) when draftKey is null", () => {
    // draftKey null -> usePersistedDraft receives null -> no localStorage touch.
    expect(localStorage.getItem("comment-draft:null")).toBeNull();
    const { container } = bar({ selection: MONO_ONE, draftKey: null });
    const textarea = getCommentInput(container);
    fireEvent.change(textarea, { target: { value: "ephemeral" } });
    expect(localStorage.getItem("comment-draft:null")).toBeNull();
    expect(textarea.value).toBe("ephemeral");
  });
});
