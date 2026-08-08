// Native-path coverage restored for three rendering.test.ts cases that
// were DELETED (not ported) as "real product gap, flagged, not fixed" —
// see phase2-inventory.md's ledger entries for `tests/rendering.test.ts`
// and `tests/runs.test.ts`. All three gaps are now closed:
//   1. Failure chrome (copy + expand/disclosure) — src/surface/leaf/Chips.tsx's
//      `FailureChip`, replacing the old flat severity-styled label.
//   2. Stopped/interrupted indicator — new src/surface/leaf/StoppedIndicator.tsx,
//      wired into TurnView.tsx for the `stopped` terminal phase.
//   3. Inline-tags-card preamble parsing — src/surface/nodes/TypedPrompt.tsx's
//      `PromptText`/`InlineTagsCards`, reusing the shared (rendering-agnostic)
//      src/utils/artificialSections.ts + src/utils/inlineTagsPrompt.ts parsers
//      legacy MessageBubble.tsx also used — no forked second parser.
//
// Component-level (no harness) for 1 and 3, matching tests/surface/
// toolInteraction.test.tsx's direct-render style; a harness-level
// integration case is added for 2 (and for 1) to prove the TurnView wiring
// itself, not just the leaf component in isolation.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import { FailureChip } from "../../src/surface/leaf/Chips";
import { StoppedIndicator } from "../../src/surface/leaf/StoppedIndicator";
import { TypedPromptView } from "../../src/surface/nodes/TypedPrompt";
import {
  compactTurn,
  failureNode,
  node,
  promptNode,
  resetSeq,
  turnLifecycleFrame,
  turnNode,
} from "./fixtures";

const copyToClipboard = vi.hoisted(() => vi.fn(async () => true));
vi.mock("../../src/utils/clipboard", () => ({ copyToClipboard }));

resetSeq();

describe("FailureChip — copy + expand/disclosure chrome (gap 3)", () => {
  it("collapses the failure text behind a toggle and expands it on click", () => {
    const payload = {
      code: "provider_error",
      text: "line one\nline two detail",
      data: null,
      severity: "error" as const,
      retryable: false,
      resolution: "none" as const,
    };
    const { container } = render(<FailureChip payload={payload} />);

    expect(container.querySelector(".error-block-body")).toBeNull();
    const toggle = container.querySelector(".error-block-toggle") as HTMLElement;
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const body = container.querySelector(".error-block-body");
    expect(body?.textContent).toBe("line one\nline two detail");
  });

  it("copies the failure text via the shared clipboard util", async () => {
    copyToClipboard.mockClear();
    const payload = {
      code: "provider_error",
      text: "boom",
      data: null,
      severity: "warning" as const,
      retryable: true,
      resolution: "none" as const,
    };
    const { container } = render(<FailureChip payload={payload} />);
    const copyBtn = container.querySelector(".error-copy-btn") as HTMLElement;
    expect(copyBtn).not.toBeNull();

    fireEvent.click(copyBtn);
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith("boom"));
    await waitFor(() => expect(copyBtn.className).toContain("copied"));
  });

  it("has no copy/expand affordance when there is no failure text", () => {
    const payload = {
      code: "other",
      text: "",
      data: null,
      severity: "info" as const,
      retryable: false,
      resolution: "none" as const,
    };
    const { container } = render(<FailureChip payload={payload} />);
    expect(container.querySelector(".error-copy-btn")).toBeNull();
    expect(container.querySelector(".error-block-toggle")?.getAttribute("aria-expanded")).toBeNull();
  });
});

describe("StoppedIndicator (gap 4)", () => {
  it("renders 'Interrupted' for a user_stopped terminal reason", () => {
    render(<StoppedIndicator reason="user_stopped" />);
    expect(screen.getByTestId("surface-stopped-indicator").textContent).toBe("Interrupted");
  });

  it("renders the generic 'Stopped' label for any other/absent terminal reason", () => {
    const { rerender } = render(<StoppedIndicator reason={null} />);
    expect(screen.getByTestId("surface-stopped-indicator").textContent).toBe("Stopped");
    rerender(<StoppedIndicator reason="provider_error" />);
    expect(screen.getByTestId("surface-stopped-indicator").textContent).toBe("Stopped");
  });
});

describe("TurnView — stopped-phase and failure-node integration (gaps 3 + 4)", () => {
  it("renders the stopped indicator when a turn's live phase reaches 'stopped'", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-stopped", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-stopped", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "go"), [])],
        });
      },
    });

    await h.selectSession("s-stopped");
    await h.waitFor(() => h.$('[data-testid="surface-turn"][data-turn-id="t1"]') !== null);
    expect(h.$('[data-testid="surface-stopped-indicator"]')).toBeNull();

    h.emitSurface("s-stopped", turnLifecycleFrame("t1", "stopped", { reason: "user_stopped" }));
    await h.waitFor(() => h.$('[data-testid="surface-stopped-indicator"]') !== null);
    expect(h.$('[data-testid="surface-stopped-indicator"]')?.textContent).toBe("Interrupted");
    h.unmount();
  });

  it("renders a failure node's chip within the live turn body", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-fail", messages: [] });
    const failNode = failureNode("t1", "fail1", "turn:t1", { text: "provider unavailable" });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-fail", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "go"), [])],
          liveTurnNodes: [failNode],
        });
      },
    });

    await h.selectSession("s-fail");
    h.emitSurface("s-fail", turnLifecycleFrame("t1", "running"));
    await h.waitFor(() => h.$('[data-testid="surface-failure-chip"]') !== null);
    expect(h.$('[data-testid="surface-failure-chip"]')?.textContent).toContain("provider unavailable");
    h.unmount();
  });
});

describe("TypedPromptView — inline-tags-card preamble parsing (gap 5)", () => {
  it("renders an <inline-tags> preamble as comment cards, not raw markup", () => {
    const text =
      "<inline-tags>\n" +
      '<c file="src/App.tsx" range="10:1-12:1"><sel>const x = 1</sel>fix this</c>\n' +
      "</inline-tags>\n" +
      "\nplease address";
    const promptNodeWithTags = node({
      node_id: "t1:prompt",
      turn_id: "t1",
      kind: "typed_prompt",
      status: "complete",
      payload: {
        text,
        attachments: [],
        send_mode: "queue",
        origin: "user",
        source_session_ref: null,
        sent_text: null,
        intent_id: null,
      },
    });

    const { container } = render(<TypedPromptView node={promptNodeWithTags} />);

    expect(container.querySelector(".inline-tags-cards")).not.toBeNull();
    expect(container.textContent).not.toContain("<inline-tags>");
    expect(container.textContent).not.toContain("<c ");
    expect(container.querySelector(".inline-tags-card-anchor")?.textContent).toBe("src/App.tsx:10:1-12:1");
    expect(container.querySelector(".inline-tags-card-selected")?.textContent).toBe("const x = 1");
    expect(container.querySelector(".comment-card-comment")?.textContent).toBe("fix this");
    expect(container.textContent).toContain("please address");
  });

  it("renders plain prompt text verbatim through Markdown when there is no inline-tags block", () => {
    const promptNodePlain = node({
      node_id: "t2:prompt",
      turn_id: "t2",
      kind: "typed_prompt",
      status: "complete",
      payload: {
        text: "just a normal prompt",
        attachments: [],
        send_mode: "queue",
        origin: "user",
        source_session_ref: null,
        sent_text: null,
        intent_id: null,
      },
    });

    const { container } = render(<TypedPromptView node={promptNodePlain} />);
    expect(container.querySelector(".inline-tags-cards")).toBeNull();
    expect(container.textContent).toContain("just a normal prompt");
  });
});
