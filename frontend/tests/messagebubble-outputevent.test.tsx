import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { OutputEvent } from "../src/components/MessageBubble";

// OutputEvent is a pure presentational dispatcher over `cleanOutput` +
// `classifyOutput`: it picks one of six render paths (null / null / error /
// success / json / tool-result / prose) and delegates rendering to the
// matching sub-component. The classification helpers themselves are covered
// in messagebubble-helpers.test.ts; this slice locks the component's own
// branch wiring — that each kind routes to the right sub-component chrome.

const renderOutput = (text: string, over: { nested?: boolean; collapsible?: boolean } = {}) => {
  const onFileClick = vi.fn();
  const view = render(
    <OutputEvent text={text} onFileClick={onFileClick} {...over} />,
  );
  return { ...view, onFileClick };
};

const has = (c: HTMLElement, sel: string) => expect(c.querySelector(sel)).not.toBeNull();
const missing = (c: HTMLElement, sel: string) => expect(c.querySelector(sel)).toBeNull();

describe("OutputEvent null paths", () => {
  it("renders nothing when the cleaned text is empty", () => {
    // 💬 prefix + zero-width only -> cleanOutput trims to "".
    const { container } = renderOutput("\u{1F4AC} ​﻿");
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for a session-start marker", () => {
    const { container } = renderOutput("📋 Session started: abc-123");
    expect(container.firstChild).toBeNull();
  });
});

describe("OutputEvent error path", () => {
  it("renders an event-error block using the parsed friendly message", () => {
    const { container } = renderOutput('❌ {"message":"rate limited"}');
    has(container, ".event-error");
    expect(container.querySelector(".event-error")?.textContent).toContain("rate limited");
  });

  it("falls back to the raw cleaned text when no friendly message can be parsed", () => {
    // classifyOutput flags it as error; parseErrorMessage finds neither a JSON
    // message nor an "API Error:" anchor -> null -> the `friendlyMsg || clean`
    // branch renders the cleaned text itself.
    const { container } = renderOutput("Failed to authenticate totally");
    has(container, ".event-error");
    expect(container.querySelector(".event-error")?.textContent).toContain("Failed to authenticate");
  });
});

describe("OutputEvent success path", () => {
  it("renders an event-success block", () => {
    const { container } = renderOutput("✅ all good");
    has(container, ".event-success");
    expect(container.querySelector(".event-success")?.textContent).toContain("all good");
  });
});

describe("OutputEvent JSON path", () => {
  it("renders a collapsible JSON block and mounts the JsonNode body when opened", () => {
    // '{"a":1}' is 7 chars -> label "JSON (7 chars)".
    const { container } = renderOutput('{"a":1}');
    const block = container.querySelector(".collapsible-output")!;
    expect(block).not.toBeNull();
    expect(container.querySelector(".collapsible-label")?.textContent).toBe("JSON (7 chars)");
    // Collapsed by default -> body (and JsonNode) not mounted yet.
    missing(container, ".raw-json");

    fireEvent.click(container.querySelector(".collapsible-output-header")!);
    // Body mounts on open, proving the JSON-path children render.
    has(container, ".raw-json.output-json");
    cleanup();
  });
});

describe("OutputEvent nested tool-result path", () => {
  it("renders a short tool result as a collapsible pre block labeled by its first line", () => {
    const { container } = renderOutput("Result: 42", { nested: true });
    has(container, ".collapsible-output");
    expect(container.querySelector(".collapsible-label")?.textContent).toBe("Result: 42");
    // Collapsed by default -> pre body not mounted.
    missing(container, ".output-pre");
    fireEvent.click(container.querySelector(".collapsible-output-header")!);
    has(container, ".output-pre");
    cleanup();
  });

  it("renders a long (>=1000 char) tool result with a truncated label and size suffix", () => {
    const text = "Result: " + "x".repeat(1000); // cleaned length 1008 -> fmtSize "1.0k"
    const { container } = renderOutput(text, { nested: true });
    const label = container.querySelector(".collapsible-label")?.textContent ?? "";
    expect(label).toContain("...");
    expect(label).toContain("(1.0k chars)");
  });

  it("renders a tool-result-shaped text as prose when NOT nested (top-level output)", () => {
    // The nested-gated isToolResult heuristic must NOT fire at top level.
    const { container } = renderOutput("Result: 42", { nested: false });
    missing(container, ".collapsible-output");
    has(container, ".message-box");
    cleanup();
  });
});

describe("OutputEvent prose path", () => {
  it("renders ordinary assistant text in a MessageBox", () => {
    const { container } = renderOutput("Here is your answer.");
    has(container, ".message-box");
    missing(container, ".event-error");
    missing(container, ".collapsible-output");
  });

  it("honors collapsible=false by rendering a static message box", () => {
    const { container } = renderOutput("Static reply.", { collapsible: false });
    has(container, ".message-box.message-box-static");
  });
});

// The collapsible MessageBox chrome: a header toggle (L497-505) flips open
// state, and the collapsed preview body (L510-517) is itself a button that
// re-opens. These tests lock the toggle handler (setOpen toggle) and the
// collapsed-body click handler (setOpen(true)) — the two interaction
// branches the prose-path rendering alone never fires.
describe("OutputEvent MessageBox collapsible toggle", () => {
  const toggle = (c: HTMLElement) => c.querySelector<HTMLButtonElement>(".message-box-toggle")!;
  const collapsedBody = (c: HTMLElement) =>
    c.querySelector<HTMLButtonElement>(".message-box-collapsed-body")!;

  it("collapses via the toggle and shows the first-line preview, then re-expands via the collapsed body", () => {
    const { container } = renderOutput("First line of reply.\nSecond line.");
    // Starts open: markdown body mounted, no collapsed preview.
    expect(toggle(container).getAttribute("aria-expanded")).toBe("true");
    has(container, ".message-box-body");
    missing(container, ".message-box-collapsed-body");

    // Toggle closes: body unmounts, collapsed preview shows the first line.
    fireEvent.click(toggle(container));
    expect(toggle(container).getAttribute("aria-expanded")).toBe("false");
    missing(container, ".message-box-body");
    expect(collapsedBody(container).textContent).toBe("First line of reply.");

    // Clicking the collapsed preview re-opens: body remounts.
    fireEvent.click(collapsedBody(container));
    expect(toggle(container).getAttribute("aria-expanded")).toBe("true");
    has(container, ".message-box-body");
    missing(container, ".message-box-collapsed-body");
    cleanup();
  });

  it("flips the toggle arrow and aria-label with open state", () => {
    const { container } = renderOutput("An answer.");
    expect(toggle(container).querySelector(".collapse-arrow")!.textContent).toBe("▼");
    expect(toggle(container).getAttribute("aria-label")).toBe("message.collapseMessageAria");

    fireEvent.click(toggle(container));
    expect(toggle(container).querySelector(".collapse-arrow")!.textContent).toBe("▶");
    expect(toggle(container).getAttribute("aria-label")).toBe("message.expandMessageAria");
    cleanup();
  });
});
