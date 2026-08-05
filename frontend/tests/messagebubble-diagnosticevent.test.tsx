import { describe, expect, it } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { DiagnosticEvent } from "../src/components/MessageBubble";

// DiagnosticEvent is the catch-all renderer for unknown/diagnostic events: it
// shows a "unknown event: <kind>" toggle that expands a <pre> with the raw
// payload pretty-printed via JSON.stringify (falling back to String(raw) when
// the value is not JSON-serializable). Branch coverage targets: the open
// ternary (chevron-right closed vs chevron-down open), the `open && <pre>`
// gate, and the try/catch around JSON.stringify (serializable vs throw).

const CHEVRON_RIGHT = "m9 18 6-6-6-6";
const CHEVRON_DOWN = "m6 9 6 6 6-6";

const renderDiag = (kind: string, raw: unknown) =>
  render(<DiagnosticEvent kind={kind} raw={raw} />);

const chevronPath = (container: HTMLElement) =>
  container.querySelector(".event-diagnostic svg path")?.getAttribute("d") ?? "";

const buttonText = (container: HTMLElement) =>
  container.querySelector(".event-diagnostic button")!.textContent ?? "";

describe("DiagnosticEvent closed state", () => {
  it("renders the kind label with a right chevron and no payload pre", () => {
    const { container } = renderDiag("custom_type", { a: 1 });
    expect(container.querySelector(".event-diagnostic")).not.toBeNull();
    // Closed -> right-pointing chevron, no expanded payload.
    expect(chevronPath(container)).toBe(CHEVRON_RIGHT);
    expect(container.querySelector(".event-diagnostic pre")).toBeNull();
    // The kind is rendered inside a <code> element, after the literal label.
    expect(buttonText(container)).toContain("unknown event:");
    expect(container.querySelector(".event-diagnostic code")!.textContent).toBe(
      "custom_type",
    );
  });
});

describe("DiagnosticEvent open state", () => {
  it("expands a pre with JSON-pretty payload after click, swapping to the down chevron", () => {
    const raw = { hello: "world", n: 3 };
    const { container } = renderDiag("stream_json", raw);
    const button = container.querySelector(".event-diagnostic button")!;
    fireEvent.click(button);
    // Open -> down chevron and the payload <pre> is present.
    expect(chevronPath(container)).toBe(CHEVRON_DOWN);
    const pre = container.querySelector(".event-diagnostic pre")!;
    expect(pre.textContent).toBe(JSON.stringify(raw, null, 2));
  });

  it("toggles back closed on a second click", () => {
    const { container } = renderDiag("t", { x: 1 });
    const button = container.querySelector(".event-diagnostic button")!;
    fireEvent.click(button);
    expect(container.querySelector(".event-diagnostic pre")).not.toBeNull();
    fireEvent.click(button);
    expect(container.querySelector(".event-diagnostic pre")).toBeNull();
    expect(chevronPath(container)).toBe(CHEVRON_RIGHT);
  });
});

describe("DiagnosticEvent non-serializable payload", () => {
  it("falls back to String(raw) when JSON.stringify throws (bigint)", () => {
    // BigInt values make JSON.stringify throw — exercises the catch branch.
    const { container } = renderDiag("bigint_event", 1n as unknown);
    fireEvent.click(container.querySelector(".event-diagnostic button")!);
    const pre = container.querySelector(".event-diagnostic pre")!;
    expect(pre.textContent).toBe(String(1n));
  });
});
