// NotesPanel renders a scrollable list of notes with inline edit,
// send-to-prompt, and remove actions. Icon has its own concerns; mock it so
// this suite isolates the panel's own render + interaction logic.
vi.mock("../src/components/Icon", () => ({
  default: ({ name, size }: { name?: string; size?: number }) =>
    React.createElement("span", { "data-icon": name ?? "", "data-size": size ?? "" }),
}));

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { NotesPanel } from "../src/components/NotesPanel";
import type { Note } from "../src/types";

// setup.ts initializes i18n with empty resources + fallbackLng: false. With a
// defaultValue passed (t(key, "default")), i18next returns the DEFAULT for a
// missing key, so titles/text render the default strings.
const T = {
  empty: "No notes yet",
  hint: "Use the note button in the input area to save text as a note",
  send: "Send to prompt",
  edit: "Edit",
  remove: "Remove",
};

function makeNote(over: Partial<Note> = {}): Note {
  return { id: "n1", text: "first note", created_at: "2026-01-01T00:00:00Z", ...over };
}

function props(over: Record<string, (...a: never[]) => void> = {}) {
  return {
    notes: [] as Note[],
    onRemove: vi.fn(),
    onEdit: vi.fn(),
    onSendToPrompt: vi.fn(),
    ...over,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

// Make the scroll container report deterministic scroll geometry. scrollTop
// is writable so both the component (auto-scroll assignment) and the test
// can move it.
function setScrollMetrics(el: Element, over: Partial<{ scrollHeight: number; clientHeight: number; scrollTop: number }> = {}) {
  const h = over.scrollHeight ?? 1000;
  const c = over.clientHeight ?? 200;
  const top = over.scrollTop ?? 0;
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => h });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => c });
  Object.defineProperty(el, "scrollTop", { configurable: true, get: () => top, set: () => {} });
  // Track writes to scrollTop so we can assert the auto-scroll assignment ran.
  let written: number | null = null;
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => (written === null ? top : written),
    set: (v: number) => { written = v; },
  });
  return {
    wasScrolled: () => written !== null,
  };
}

describe("NotesPanel", () => {
  it("renders empty state when there are no notes", () => {
    render(<NotesPanel {...props()} />);
    expect(screen.getByText(T.empty)).toBeTruthy();
    expect(screen.getByText(T.hint)).toBeTruthy();
    expect(screen.queryByText("first note")).toBeNull();
  });

  it("renders one card per note with its text", () => {
    const notes = [makeNote({ id: "a", text: "alpha" }), makeNote({ id: "b", text: "beta" })];
    render(<NotesPanel {...props()} notes={notes} />);
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();
  });

  it("enters edit mode prefilled with the note text", () => {
    const p = props();
    render(<NotesPanel {...p} notes={[makeNote()]} />);
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note") as HTMLTextAreaElement;
    expect(ta.tagName).toBe("TEXTAREA");
  });

  it("commits a trimmed edit on blur", () => {
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onEdit })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note");
    fireEvent.change(ta, { target: { value: "  updated  " } });
    fireEvent.blur(ta);
    expect(onEdit).toHaveBeenCalledWith("n1", "updated");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("does not commit a blank edit on blur but exits edit mode", () => {
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onEdit })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note");
    fireEvent.change(ta, { target: { value: "   " } });
    fireEvent.blur(ta);
    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("commits a trimmed edit on Enter (without shift)", () => {
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onEdit })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note");
    fireEvent.change(ta, { target: { value: "via-enter" } });
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
    expect(onEdit).toHaveBeenCalledWith("n1", "via-enter");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("does not commit on Shift+Enter", () => {
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onEdit })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("cancels the edit on Escape without committing", () => {
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onEdit })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    const ta = screen.getByDisplayValue("first note");
    fireEvent.change(ta, { target: { value: "discarded" } });
    fireEvent.keyDown(ta, { key: "Escape" });
    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("send-to-prompt button fires onSendToPrompt and discards an in-flight edit", () => {
    const onSendToPrompt = vi.fn();
    const onEdit = vi.fn();
    render(<NotesPanel {...props({ onSendToPrompt, onEdit })} notes={[makeNote()]} />);
    // Enter edit mode so an in-flight edit exists, then trigger send — the
    // action fires and the pending edit is discarded (not committed).
    fireEvent.mouseDown(screen.getByTitle(T.edit));
    fireEvent.mouseDown(screen.getByTitle(T.send));
    expect(onSendToPrompt).toHaveBeenCalledWith("n1", "first note");
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("remove button fires onRemove", () => {
    const onRemove = vi.fn();
    render(<NotesPanel {...props({ onRemove })} notes={[makeNote()]} />);
    fireEvent.mouseDown(screen.getByTitle(T.remove));
    expect(onRemove).toHaveBeenCalledWith("n1");
  });

  it("auto-scrolls to bottom on render while stuck to bottom", () => {
    const { container } = render(<NotesPanel {...props()} notes={[makeNote()]} />);
    const panel = container.querySelector(".notes-panel") as HTMLElement;
    const metrics = setScrollMetrics(panel, { scrollTop: 100, scrollHeight: 1000, clientHeight: 200 });
    // Re-render to re-run the [notes, stickToBottom] effect while stuck.
    render(<NotesPanel {...props()} notes={[makeNote()]} />, { container });
    expect(metrics.wasScrolled()).toBe(true);
  });

  it("turns stick off when scrolled away from the bottom, then skips auto-scroll", () => {
    const { container, rerender } = render(<NotesPanel {...props()} notes={[makeNote()]} />);
    const panel = container.querySelector(".notes-panel") as HTMLElement;
    setScrollMetrics(panel, { scrollTop: 0, scrollHeight: 1000, clientHeight: 200 });
    // Scrolled to the top → not at bottom → stick turns off.
    fireEvent.scroll(panel);
    // Add a note while stick is off → effect must early-return (no auto-scroll).
    const metrics2 = setScrollMetrics(panel, { scrollTop: 0, scrollHeight: 1200, clientHeight: 200 });
    rerender(<NotesPanel {...props()} notes={[makeNote({ id: "n2", text: "second" })]} />);
    expect(metrics2.wasScrolled()).toBe(false);
    // Scrolling back to the bottom re-enables stick.
    setScrollMetrics(panel, { scrollTop: 1000, scrollHeight: 1200, clientHeight: 200 });
    fireEvent.scroll(panel);
    rerender(<NotesPanel {...props()} notes={[makeNote({ id: "n2", text: "second" })]} />);
    // Stick re-enabled → next render auto-scrolls again.
    const metrics3 = setScrollMetrics(panel, { scrollTop: 0, scrollHeight: 1200, clientHeight: 200 });
    rerender(<NotesPanel {...props()} notes={[makeNote({ id: "n3", text: "third" })]} />);
    expect(metrics3.wasScrolled()).toBe(true);
  });
});
