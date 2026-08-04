// ProjectSuggestionModal is a pure presentational modal that asks whether to
// relocate a misrouted session. No internal async state — all behavior is
// callback wiring + computed display (confidence %, names, cwd). i18n keys
// are passed to t() with NO defaultValue, so under empty-resources config the
// KEY string renders verbatim (gotcha-24/31); interpolation options are
// ignored because the resolved string (the key) has no {{project}} slot.
import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ProjectSuggestion } from "../src/components/ProjectSuggestionModal";
import { ProjectSuggestionModal } from "../src/components/ProjectSuggestionModal";

const SEND_HERE_LABEL = "projectSuggestion.sendTo";
const MOVE_LABEL = "projectSuggestion.moveTo";

const SUGGESTION: ProjectSuggestion = {
  target_cwd: "/home/me/real-project",
  score: 0.837,
  margin: 0.4,
};

const noop = () => {};

function renderModal(overrides: Partial<Parameters<typeof ProjectSuggestionModal>[0]> = {}) {
  const onMove = overrides.onMove ?? vi.fn();
  const onSendHere = overrides.onSendHere ?? vi.fn();
  const onCancel = overrides.onCancel ?? vi.fn();
  render(
    <ProjectSuggestionModal
      suggestion={overrides.suggestion ?? SUGGESTION}
      currentName={overrides.currentName ?? "Wrong Project"}
      targetName={overrides.targetName ?? "Right Project"}
      onMove={onMove}
      onSendHere={onSendHere}
      onCancel={onCancel}
    />,
  );
  return { onMove, onSendHere, onCancel };
}

describe("ProjectSuggestionModal", () => {
  it("renders the header, both project names, the rounded confidence match, and the target cwd", () => {
    renderModal({ currentName: "Alpha", targetName: "Beta" });
    expect(screen.getByText("Check project")).toBeTruthy();
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
    // Math.round(0.837 * 100) === 84
    expect(screen.getByText(/\(84% match\)/)).toBeTruthy();
    expect(screen.getByText(SUGGESTION.target_cwd)).toBeTruthy();
  });

  it("rounds confidence to a whole percent (0.5 -> 50)", () => {
    renderModal({ suggestion: { target_cwd: "/x", score: 0.5, margin: 0.1 } });
    expect(screen.getByText(/\(50% match\)/)).toBeTruthy();
  });

  it("renders the send-here and move button labels verbatim (i18n keys)", () => {
    renderModal();
    expect(screen.getByText(SEND_HERE_LABEL)).toBeTruthy();
    expect(screen.getByText(MOVE_LABEL)).toBeTruthy();
  });

  it("clicking the overlay calls onCancel", () => {
    const { onCancel } = renderModal();
    fireEvent.click(screen.getByText("Check project").closest(".modal-overlay")!);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the modal content does not close (stopPropagation)", () => {
    const { onCancel } = renderModal();
    fireEvent.click(screen.getByText("Check project").closest(".modal-content")!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("the close (×) button calls onCancel", () => {
    const { onCancel } = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "×" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("the send-here button calls onSendHere", () => {
    const { onSendHere } = renderModal();
    fireEvent.click(screen.getByText(SEND_HERE_LABEL).closest("button")!);
    expect(onSendHere).toHaveBeenCalledTimes(1);
  });

  it("the move button calls onMove", () => {
    const { onMove } = renderModal();
    fireEvent.click(screen.getByText(MOVE_LABEL).closest("button")!);
    expect(onMove).toHaveBeenCalledTimes(1);
  });

  it("wires the hardware/browser back button to onCancel (popstate)", () => {
    const { onCancel } = renderModal();
    expect(onCancel).not.toHaveBeenCalled();
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
