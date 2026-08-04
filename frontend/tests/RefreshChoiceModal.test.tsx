// RefreshChoiceModal is a pure presentational modal that lets the user pick
// how to apply a backend restart (now vs idle). Icon has its own concerns;
// mock it so this suite isolates the modal's own click logic. i18n keys are
// passed to t() with NO defaultValue, so under empty-resources config the KEY
// string renders verbatim (gotcha-24/31) — assert against the keys.
vi.mock("../src/components/Icon", () => ({
  default: ({ name, size }: { name?: string; size?: number }) =>
    React.createElement("span", { "data-icon": name ?? "", "data-size": size ?? "" }),
}));

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RefreshChoiceModal } from "../src/components/RefreshChoiceModal";

const T = {
  title: "app.refreshModalTitle",
  nowTitle: "app.refreshNowTitle",
  nowDesc: "app.refreshNowDescription",
  idleTitle: "app.refreshIdleTitle",
  idleDesc: "app.refreshIdleDescription",
  cancel: "app.cancel",
  dismiss: "startup_tasks.dismiss",
};

const noop = () => {};

describe("RefreshChoiceModal", () => {
  it("renders the title, both options, and the cancel button", () => {
    render(<RefreshChoiceModal onRefresh={noop} onClose={noop} />);
    expect(screen.getByText(T.title)).toBeTruthy();
    expect(screen.getByText(T.nowTitle)).toBeTruthy();
    expect(screen.getByText(T.nowDesc)).toBeTruthy();
    expect(screen.getByText(T.idleTitle)).toBeTruthy();
    expect(screen.getByText(T.idleDesc)).toBeTruthy();
    expect(screen.getByText(T.cancel)).toBeTruthy();
    expect(screen.getByLabelText(T.dismiss)).toBeTruthy();
  });

  it("clicking the overlay calls onClose", () => {
    const onClose = vi.fn();
    render(<RefreshChoiceModal onRefresh={noop} onClose={onClose} />);
    // The overlay is the outermost element.
    fireEvent.click(screen.getByText(T.title).closest(".modal-overlay")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the modal content does not close (stopPropagation)", () => {
    const onClose = vi.fn();
    render(<RefreshChoiceModal onRefresh={noop} onClose={onClose} />);
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("the close (×) button calls onClose", () => {
    const onClose = vi.fn();
    render(<RefreshChoiceModal onRefresh={noop} onClose={onClose} />);
    fireEvent.click(screen.getByLabelText(T.dismiss));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("the footer cancel button calls onClose", () => {
    const onClose = vi.fn();
    render(<RefreshChoiceModal onRefresh={noop} onClose={onClose} />);
    fireEvent.click(screen.getByText(T.cancel));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicking the refresh-now option calls onRefresh with 'now'", () => {
    const onRefresh = vi.fn();
    render(<RefreshChoiceModal onRefresh={onRefresh} onClose={noop} />);
    fireEvent.click(screen.getByText(T.nowTitle).closest("button.refresh-choice-option")!);
    expect(onRefresh).toHaveBeenCalledWith("now");
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("clicking the refresh-idle option calls onRefresh with 'idle'", () => {
    const onRefresh = vi.fn();
    render(<RefreshChoiceModal onRefresh={onRefresh} onClose={noop} />);
    fireEvent.click(screen.getByText(T.idleTitle).closest("button.refresh-choice-option")!);
    expect(onRefresh).toHaveBeenCalledWith("idle");
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});
