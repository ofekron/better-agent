// PreSendAdvisoryDialog is a modal that renders extension pre-send advisories
// and exposes cancel / snooze-5h / send-anyway paths. useBackButtonDismiss has
// its own test; mock it to a spy here so this suite isolates the dialog's own
// render + click logic AND can assert it is wired with (open, onCancel).
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: vi.fn(),
}));

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useBackButtonDismiss } from "../src/hooks/useBackButtonDismiss";
import { PreSendAdvisoryDialog } from "../src/components/PreSendAdvisoryDialog";
import type { PreSendAdvisory } from "../src/utils/preSendAdvisory";

// i18n is initialized in setup.ts with empty resources + fallbackLng: false,
// so t(key) returns the key string verbatim. For t(key, { time }) the
// interpolation arg is dropped (no resource to interpolate into) and the key
// itself is rendered — so the formatted date value is NOT visible in the DOM,
// only the PRESENCE of the resets line is. That presence is the observable
// behavior of formatResetsAt's null-vs-string return.
const T = {
  title: "preSendAdvisory.title",
  cancel: "preSendAdvisory.cancel",
  snooze: "preSendAdvisory.snoozeFiveHours",
  send: "preSendAdvisory.sendAnyway",
  resetsAt: "preSendAdvisory.resetsAt",
};

const noop = () => {};

function makeAdvisory(over: Partial<PreSendAdvisory> = {}): PreSendAdvisory {
  return {
    extension_id: "ext-a",
    title: "Quota low",
    severity: "warn",
    ...over,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreSendAdvisoryDialog", () => {
  it("renders nothing and skips body when open is false", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <PreSendAdvisoryDialog
        open={false}
        advisories={[makeAdvisory()]}
        onSendAnyway={noop}
        onCancel={onCancel}
        onSnoozeFiveHours={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
    // The hook is still wired so a parent flipping open→false can tear down.
    expect(useBackButtonDismiss).toHaveBeenCalledWith(false, onCancel);
  });

  it("renders the modal body and wires the back-button hook when open", () => {
    const onCancel = vi.fn();
    render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[makeAdvisory()]}
        onSendAnyway={noop}
        onCancel={onCancel}
        onSnoozeFiveHours={noop}
      />,
    );
    expect(screen.getByText(T.title)).toBeTruthy();
    expect(useBackButtonDismiss).toHaveBeenCalledWith(true, onCancel);
  });

  it("clicking the overlay cancels", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[]}
        onSendAnyway={noop}
        onCancel={onCancel}
        onSnoozeFiveHours={noop}
      />,
    );
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the modal content does NOT cancel (stopPropagation)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[]}
        onSendAnyway={noop}
        onCancel={onCancel}
        onSnoozeFiveHours={noop}
      />,
    );
    // fireEvent bubbles by default; without stopPropagation this would reach
    // the overlay handler. Asserting onCancel stays untouched proves the guard.
    fireEvent.click(container.querySelector(".modal-content")!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("the × header button, footer cancel, snooze, and send-anyway each fire their callback", () => {
    const onCancel = vi.fn();
    const onSnooze = vi.fn();
    const onSend = vi.fn();
    render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[]}
        onSendAnyway={onSend}
        onCancel={onCancel}
        onSnoozeFiveHours={onSnooze}
      />,
    );
    fireEvent.click(screen.getByText("×"));
    expect(onCancel).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText(T.cancel));
    expect(onCancel).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByText(T.snooze));
    expect(onSnooze).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText(T.send));
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("renders each advisory with its title and optional detail", () => {
    render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[
          makeAdvisory({ extension_id: "ext-a", title: "Quota low", detail: "90% used" }),
          makeAdvisory({ extension_id: "ext-b", title: "No detail here" }),
        ]}
        onSendAnyway={noop}
        onCancel={noop}
        onSnoozeFiveHours={noop}
      />,
    );
    expect(screen.getByText("Quota low")).toBeTruthy();
    expect(screen.getByText("90% used")).toBeTruthy();
    expect(screen.getByText("No detail here")).toBeTruthy();
  });

  it("shows the resets line only when resets_at is a valid date", () => {
    const { rerender } = render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[makeAdvisory({ resets_at: "not-a-real-date" })]}
        onSendAnyway={noop}
        onCancel={noop}
        onSnoozeFiveHours={noop}
      />,
    );
    // Invalid date -> formatResetsAt returns null -> resets line absent.
    expect(screen.queryByText(T.resetsAt)).toBeNull();

    rerender(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[makeAdvisory({ resets_at: "2025-01-15T10:30:00Z" })]}
        onSendAnyway={noop}
        onCancel={noop}
        onSnoozeFiveHours={noop}
      />,
    );
    // Valid date -> formatResetsAt returns a string -> resets line present.
    expect(screen.getByText(T.resetsAt)).toBeTruthy();
  });

  it("omits the resets line when resets_at is missing", () => {
    render(
      <PreSendAdvisoryDialog
        open={true}
        advisories={[makeAdvisory({ resets_at: undefined })]}
        onSendAnyway={noop}
        onCancel={noop}
        onSnoozeFiveHours={noop}
      />,
    );
    // undefined -> formatResetsAt returns null -> resets line absent.
    expect(screen.queryByText(T.resetsAt)).toBeNull();
  });
});
