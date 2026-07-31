import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createRef } from "react";
import { ScheduleSendPopover, type ScheduleSendPayload } from "../src/components/ScheduleSendPopover";

/** Renders the popover attached to an outside anchor so the
 * "click on anchor does not close" branch is reachable. */
function renderWithAnchor(onClose = vi.fn(), onSchedule: (p: ScheduleSendPayload) => Promise<boolean> | boolean = vi.fn(async () => true), prompt = "send the report") {
  const anchorRef = createRef<HTMLDivElement>();
  const utils = render(
    <div>
      <div ref={anchorRef} data-testid="anchor" />
      <ScheduleSendPopover prompt={prompt} anchorRef={anchorRef} onClose={onClose} onSchedule={onSchedule} />
    </div>,
  );
  return { ...utils, anchorRef, onClose, onSchedule };
}

const DATETIME_LOCAL = /^\d{4}-\d{2}-\d{2}T\d{2}:00$/;

describe("ScheduleSendPopover — initial render", () => {
  it("shows the title, prompt preview, and a default datetime-local value rounded to the hour", () => {
    renderWithAnchor();
    const dialog = screen.getByTestId("schedule-popover");
    expect(dialog.getAttribute("aria-label")).toBe("schedule.title");
    expect(screen.getByText("schedule.title")).toBeTruthy();
    expect(screen.getByText("send the report")).toBeTruthy();
    expect(screen.getByText("send the report").closest("[title]")).toBeTruthy();

    const fireAt = screen.getByTestId("schedule-fire-at") as HTMLInputElement;
    expect(fireAt.value).toMatch(DATETIME_LOCAL);
    // submit enabled when prompt + time present
    expect((screen.getByTestId("schedule-submit") as HTMLButtonElement).disabled).toBe(false);
  });

  it("disables submit and shows no prompt preview when the prompt is blank", () => {
    renderWithAnchor(vi.fn(), vi.fn(async () => true), "   ");
    const submit = screen.getByTestId("schedule-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    const preview = screen.getByTestId("schedule-popover").querySelector(".schedule-popover-prompt");
    expect(preview?.textContent).toBe("");
  });
});

describe("ScheduleSendPopover — field changes", () => {
  it("disables submit when the datetime is cleared", () => {
    renderWithAnchor();
    const fireAt = screen.getByTestId("schedule-fire-at") as HTMLInputElement;
    fireEvent.change(fireAt, { target: { value: "" } });
    expect((screen.getByTestId("schedule-submit") as HTMLButtonElement).disabled).toBe(true);
  });

  it("updates fire_at from the datetime input", () => {
    const onSchedule = vi.fn(async () => true);
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.change(screen.getByTestId("schedule-fire-at"), { target: { value: "2030-01-02T13:00" } });
    fireEvent.click(screen.getByTestId("schedule-submit"));
    expect(onSchedule.mock.calls[0][0].fire_at).toBe("2030-01-02T13:00");
  });

  it("maps the hourly mode to recurring/3600", () => {
    const onSchedule = vi.fn(async () => false);
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.change(screen.getByTestId("schedule-repeat"), { target: { value: "hourly" } });
    fireEvent.click(screen.getByTestId("schedule-submit"));
    expect(onSchedule.mock.calls[0][0]).toMatchObject({ kind: "recurring", interval_seconds: 3600 });
  });

  it("maps the daily mode to recurring/86400", () => {
    const onSchedule = vi.fn(async () => false);
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.change(screen.getByTestId("schedule-repeat"), { target: { value: "daily" } });
    fireEvent.click(screen.getByTestId("schedule-submit"));
    expect(onSchedule.mock.calls[0][0]).toMatchObject({ kind: "recurring", interval_seconds: 86400 });
  });

  it("maps the once mode to once/null", () => {
    const onSchedule = vi.fn(async () => false);
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.change(screen.getByTestId("schedule-repeat"), { target: { value: "once" } });
    fireEvent.click(screen.getByTestId("schedule-submit"));
    expect(onSchedule.mock.calls[0][0]).toMatchObject({ kind: "once", interval_seconds: null });
  });

  it("passes the trimmed prompt in the payload", () => {
    const onSchedule = vi.fn(async () => true);
    renderWithAnchor(vi.fn(), onSchedule, "  trim me  ");
    fireEvent.click(screen.getByTestId("schedule-submit"));
    expect(onSchedule.mock.calls[0][0].prompt).toBe("trim me");
  });
});

describe("ScheduleSendPopover — submit lifecycle", () => {
  it("closes after a successful schedule (ok true)", async () => {
    const onClose = vi.fn();
    const onSchedule = vi.fn(async () => true);
    renderWithAnchor(onClose, onSchedule);
    fireEvent.click(screen.getByTestId("schedule-submit"));
    await new Promise((r) => setTimeout(r, 0));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does NOT close when onSchedule resolves false", async () => {
    const onClose = vi.fn();
    const onSchedule = vi.fn(async () => false);
    renderWithAnchor(onClose, onSchedule);
    fireEvent.click(screen.getByTestId("schedule-submit"));
    await new Promise((r) => setTimeout(r, 0));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("surfaces an Error message and keeps the popover open on rejection", async () => {
    const onClose = vi.fn();
    const onSchedule = vi.fn(async () => {
      throw new Error("past time");
    });
    renderWithAnchor(onClose, onSchedule);
    fireEvent.click(screen.getByTestId("schedule-submit"));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("past time")).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("surfaces a non-Error rejection as a string", async () => {
    const onSchedule = vi.fn(async () => {
      throw "string-boom";
    });
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.click(screen.getByTestId("schedule-submit"));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("string-boom")).toBeTruthy();
  });

  it("shows the scheduling label and disables submit while submitting", async () => {
    let resolveSchedule!: (v: boolean) => void;
    const onSchedule = vi.fn(
      () =>
        new Promise<boolean>((res) => {
          resolveSchedule = res;
        }),
    );
    renderWithAnchor(vi.fn(), onSchedule);
    fireEvent.click(screen.getByTestId("schedule-submit"));
    // While the promise is pending the label flips and submit disables.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("schedule.scheduling")).toBeTruthy();
    expect((screen.getByTestId("schedule-submit") as HTMLButtonElement).disabled).toBe(true);
    resolveSchedule(true);
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("schedule.scheduleSend")).toBeTruthy();
  });
});

describe("ScheduleSendPopover — close interactions", () => {
  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores non-Escape keys", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.keyDown(document, { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes on a mousedown outside panel and anchor", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close on mousedown inside the panel", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.mouseDown(screen.getByTestId("schedule-popover"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not close on mousedown on the anchor", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.mouseDown(screen.getByTestId("anchor"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("removes its document listeners after unmount", () => {
    const onClose = vi.fn();
    const { unmount } = renderWithAnchor(onClose);
    unmount();
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.mouseDown(document.body);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes via the header close button", () => {
    const onClose = vi.fn();
    renderWithAnchor(onClose);
    fireEvent.click(screen.getByLabelText("app.cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("ScheduleSendPopover — drag", () => {
  beforeEach(() => {
    // happy-dom may omit the pointer-capture API on elements; provide a no-op
    // so the handlers that call setPointerCapture/releasePointerCapture run.
    if (!Element.prototype.setPointerCapture) {
      Element.prototype.setPointerCapture = function () {};
    }
    if (!Element.prototype.releasePointerCapture) {
      Element.prototype.releasePointerCapture = function () {};
    }
  });

  it("moves the panel by the pointer delta during a drag", () => {
    renderWithAnchor();
    const handle = screen.getByTestId("schedule-popover-drag-handle");
    const panel = screen.getByTestId("schedule-popover");
    expect(panel.style.transform).toBe("translate(0px, 0px)");

    fireEvent.pointerDown(handle, { clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 130, clientY: 90, pointerId: 1 });
    expect(panel.style.transform).toBe("translate(30px, -10px)");

    fireEvent.pointerUp(handle, { clientX: 130, clientY: 90, pointerId: 1 });
    // pointer move after release is a no-op (dragState cleared)
    fireEvent.pointerMove(handle, { clientX: 200, clientY: 200, pointerId: 1 });
    expect(panel.style.transform).toBe("translate(30px, -10px)");
  });

  it("ends the drag on pointer cancel", () => {
    renderWithAnchor();
    const handle = screen.getByTestId("schedule-popover-drag-handle");
    fireEvent.pointerDown(handle, { clientX: 0, clientY: 0, pointerId: 2 });
    fireEvent.pointerCancel(handle, { clientX: 0, clientY: 0, pointerId: 2 });
    fireEvent.pointerMove(handle, { clientX: 50, clientY: 50, pointerId: 2 });
    expect(screen.getByTestId("schedule-popover").style.transform).toBe("translate(0px, 0px)");
  });

  it("does not start a drag when pointer down originates on the close button", () => {
    renderWithAnchor();
    const panel = screen.getByTestId("schedule-popover");
    // Dispatch from the close button: e.target.closest('.schedule-popover-close') matches → early return.
    fireEvent.pointerDown(screen.getByLabelText("app.cancel"), { clientX: 5, clientY: 5, pointerId: 3 });
    fireEvent.pointerMove(screen.getByLabelText("app.cancel"), { clientX: 80, clientY: 80, pointerId: 3 });
    expect(panel.style.transform).toBe("translate(0px, 0px)");
  });

  it("ignores pointer move/up when no drag is active", () => {
    renderWithAnchor();
    const handle = screen.getByTestId("schedule-popover-drag-handle");
    const panel = screen.getByTestId("schedule-popover");
    fireEvent.pointerMove(handle, { clientX: 50, clientY: 50, pointerId: 4 });
    fireEvent.pointerUp(handle, { clientX: 50, clientY: 50, pointerId: 4 });
    expect(panel.style.transform).toBe("translate(0px, 0px)");
  });
});
