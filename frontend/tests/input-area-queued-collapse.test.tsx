import fs from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "../src/i18n";
import { InputArea } from "../src/components/InputArea";

const COLLAPSE_KEY = "better-agent-queued-list-collapsed";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function renderQueue(count: number) {
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
    />,
  );
}

describe("InputArea queued list collapse", () => {
  it("collapses all queued banners to a summary and persists the choice", () => {
    renderQueue(2);

    expect(screen.getAllByTestId("queued-prompt-banner")).toHaveLength(2);
    expect(screen.getByTestId("queued-list-summary").textContent).toBe(
      "2 queued prompts",
    );

    const toggle = screen.getByTestId("queued-list-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);

    expect(screen.queryAllByTestId("queued-prompt-banner")).toHaveLength(0);
    expect(screen.getByTestId("queued-list-summary").textContent).toBe(
      "2 queued prompts",
    );
    expect(screen.getByTestId("queued-list-toggle").getAttribute("aria-expanded")).toBe(
      "false",
    );
    expect(window.localStorage.getItem(COLLAPSE_KEY)).toBe("true");

    cleanup();
    renderQueue(2);
    expect(screen.queryAllByTestId("queued-prompt-banner")).toHaveLength(0);
    expect(screen.getByTestId("queued-list-summary").textContent).toBe(
      "2 queued prompts",
    );
  });

  it("expands back via the summary text and shows the banners again", () => {
    window.localStorage.setItem(COLLAPSE_KEY, "true");
    renderQueue(3);

    expect(screen.queryAllByTestId("queued-prompt-banner")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("queued-list-summary"));

    expect(screen.getAllByTestId("queued-prompt-banner")).toHaveLength(3);
    expect(window.localStorage.getItem(COLLAPSE_KEY)).toBe("false");
  });

  it("uses the singular summary for one queued prompt", () => {
    renderQueue(1);
    expect(screen.getByTestId("queued-list-summary").textContent).toBe(
      "1 queued prompt",
    );
  });

  it("renders no queue header when nothing is queued", () => {
    render(
      <InputArea
        onSend={vi.fn()}
        onInterrupt={vi.fn()}
        isStreaming={false}
        disabled={false}
        draft=""
        onDraftChange={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("queued-list-header")).toBeNull();
  });
});

describe("queued list collapse CSS", () => {
  const css = fs.readFileSync(
    path.resolve(__dirname, "../src/styles/globals.css"),
    "utf8",
  );

  it("defines the header, summary, and enter animation rules", () => {
    expect(css).toMatch(/\.queued-list-header\s*\{[^}]*display:\s*flex/);
    expect(css).toMatch(/\.queued-list-summary\s*\{[^}]*cursor:\s*pointer/);
    expect(css).toMatch(/@keyframes queued-banner-in/);
    expect(css).toMatch(
      /\.queued-prompt-banner\s*\{[^}]*animation:\s*queued-banner-in/,
    );
  });

  it("disables the banner animation under reduced motion", () => {
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.queued-prompt-banner\s*\{[^}]*animation:\s*none/,
    );
  });
});
