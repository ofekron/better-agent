import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../src/i18n";
import { InputArea } from "../src/components/InputArea";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function renderQueue(
  count: number,
  overrides: Partial<React.ComponentProps<typeof InputArea>> = {},
) {
  const queuedPrompts = Array.from({ length: count }, (_, i) => ({
    id: `q${i + 1}`,
    preview: `queued work ${i + 1}`,
  }));
  return render(
    <InputArea
      onSend={vi.fn()}
      onInterrupt={vi.fn()}
      isStreaming={true}
      disabled={false}
      draft=""
      onDraftChange={vi.fn()}
      queuedPrompts={queuedPrompts}
      onPromoteQueued={vi.fn()}
      onCancelQueued={vi.fn()}
      {...overrides}
    />,
  );
}

describe("InputArea queued save-to-note", () => {
  it("scopes onQueuedToNote to the exact item saved, not the whole queue", () => {
    // Regression test: InputArea used to pass onQueuedToNote straight
    // through with no item id, so the App-level handler had no way to
    // cancel just that one item and fell back to wiping the whole queue.
    const onQueuedToNote = vi.fn();
    renderQueue(3, { onQueuedToNote });

    const noteButtons = screen.getAllByTitle("Save to notes");
    fireEvent.click(noteButtons[1]);

    expect(onQueuedToNote).toHaveBeenCalledWith("queued work 2", "q2");
    expect(onQueuedToNote).not.toHaveBeenCalledWith("queued work 2", undefined);
  });
});

describe("InputArea queue bulk actions", () => {
  it("hides bulk controls with a single queued item", () => {
    renderQueue(1);
    expect(screen.queryByTestId("queued-list-bulk-actions")).toBeNull();
    expect(screen.queryByTestId("queued-select-checkbox")).toBeNull();
  });

  it("cancels all queued items via the header when nothing is selected", () => {
    const onCancelQueued = vi.fn();
    renderQueue(3, { onCancelQueued });

    fireEvent.click(screen.getByTestId("queued-bulk-cancel"));
    expect(onCancelQueued).toHaveBeenCalledWith();
  });

  it("cancels only the selected items via the header", () => {
    const onCancelQueued = vi.fn();
    renderQueue(3, { onCancelQueued });

    const checkboxes = screen.getAllByTestId("queued-select-checkbox");
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[2]);

    expect(screen.getByTestId("queued-bulk-cancel").textContent).toContain("2");
    fireEvent.click(screen.getByTestId("queued-bulk-cancel"));

    expect(onCancelQueued).toHaveBeenCalledWith("q1");
    expect(onCancelQueued).toHaveBeenCalledWith("q3");
    expect(onCancelQueued).not.toHaveBeenCalledWith("q2");
    expect(onCancelQueued).not.toHaveBeenCalledWith();
  });

  it("interrupts with every queued id when nothing is selected", () => {
    const onPromoteQueuedMulti = vi.fn();
    renderQueue(3, { onPromoteQueuedMulti });

    fireEvent.click(screen.getByTestId("queued-bulk-interrupt"));
    expect(onPromoteQueuedMulti).toHaveBeenCalledWith(["q1", "q2", "q3"]);
  });

  it("interrupts with only the selected queued ids", () => {
    const onPromoteQueuedMulti = vi.fn();
    renderQueue(3, { onPromoteQueuedMulti });

    const checkboxes = screen.getAllByTestId("queued-select-checkbox");
    fireEvent.click(checkboxes[1]);

    expect(screen.getByTestId("queued-bulk-interrupt").textContent).toContain("1");
    fireEvent.click(screen.getByTestId("queued-bulk-interrupt"));

    expect(onPromoteQueuedMulti).toHaveBeenCalledWith(["q2"]);
  });
});
