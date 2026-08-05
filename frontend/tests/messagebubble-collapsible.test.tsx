import { describe, expect, it } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import * as React from "react";
import {
  CollapsibleOutput,
  CollapsibleTimelineBlock,
} from "../src/components/MessageBubble";
import type { WSEvent } from "../src/types";

// These two components are presentational collapse/expand shells. In
// production both are reached only through renderers that fix `defaultOpen`
// (CollapsibleOutput is always mounted defaultOpen=true from OutputEvent;
// CollapsibleTimelineBlock's toggle is exercised only through grouped
// supervisor timelines). We mount them directly so every branch of their
// internal open/close state is covered: collapsed vs expanded chrome, the
// toggle click, the empty/no-expand static header, the collapsed preview, and
// the created/anchor decoration. Assertions rest on the eager chrome
// (headers, arrows, counts, body, ellipsis) — never on lazy tool content.

const ev = (e: Partial<WSEvent>) => e as WSEvent;

const outputEvent = (output: string): WSEvent =>
  ev({ type: "output", data: { output } });

const renderTimeline = (
  props: Partial<Parameters<typeof CollapsibleTimelineBlock>[0]>,
) =>
  render(
    <CollapsibleTimelineBlock
      label="worker-1"
      chipLabel="WORKER"
      events={[outputEvent("did work")]}
      {...props}
    />,
  ).container;

describe("CollapsibleOutput open/close state machine", () => {
  it("renders collapsed by default (no body, ▶ arrow)", () => {
    const c = render(
      <CollapsibleOutput label="logs">{"payload"}</CollapsibleOutput>,
    ).container;
    expect(c.querySelector(".collapsible-output-body")).toBeNull();
    expect(c.querySelector(".collapsible-arrow")?.textContent).toBe("▶");
    expect(c.querySelector(".collapsible-label")?.textContent).toBe("logs");
  });

  it("renders the body when defaultOpen (▼ arrow)", () => {
    const c = render(
      <CollapsibleOutput label="logs" defaultOpen>
        {"payload"}
      </CollapsibleOutput>,
    ).container;
    expect(c.querySelector(".collapsible-output-body")).not.toBeNull();
    expect(c.querySelector(".collapsible-arrow")?.textContent).toBe("▼");
  });

  it("expands on header click when collapsed", () => {
    const c = render(
      <CollapsibleOutput label="logs">{"payload"}</CollapsibleOutput>,
    ).container;
    fireEvent.click(c.querySelector(".collapsible-output-header")!);
    expect(c.querySelector(".collapsible-output-body")).not.toBeNull();
    expect(c.querySelector(".collapsible-arrow")?.textContent).toBe("▼");
  });

  it("collapses on header click when open", () => {
    const c = render(
      <CollapsibleOutput label="logs" defaultOpen>
        {"payload"}
      </CollapsibleOutput>,
    ).container;
    fireEvent.click(c.querySelector(".collapsible-output-header")!);
    expect(c.querySelector(".collapsible-output-body")).toBeNull();
    expect(c.querySelector(".collapsible-arrow")?.textContent).toBe("▶");
  });
});

describe("CollapsibleTimelineBlock expand/collapse + decoration", () => {
  it("shows a static header (no toggle) when nothing is expandable", () => {
    const c = renderTimeline({
      events: [
        ev({ type: "complete", data: {} }),
        ev({ type: "session_discovered", data: {} }),
      ],
    });
    expect(c.querySelector(".timeline-toggle-header")).toBeNull();
    expect(c.querySelector(".timeline-static-header")).not.toBeNull();
    expect(c.querySelector(".timeline-block-body")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).toBeNull();
  });

  it("omits the thread-dot and chip span when labelColor/chipClass are absent", () => {
    // Only required props: labelColor && and chipClass ? branches both false.
    const c = renderTimeline({ events: [outputEvent("x")] });
    expect(c.querySelector(".thread-dot")).toBeNull();
    expect(c.querySelector(".timeline-toggle-label")?.textContent).toBe("worker-1");
    // no chip span is rendered without a chipClass
    expect(c.querySelector(".timeline-toggle-header > span.worker-chip")).toBeNull();
  });

  it("renders collapsed count, chip, dot, and preview when closed", () => {
    const c = renderTimeline({
      label: "sub-agent",
      labelColor: "rgb(10,20,30)",
      chipClass: "worker-chip",
      defaultOpen: false,
    });
    const header = c.querySelector(".timeline-toggle-header")!;
    expect(header).not.toBeNull();
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(c.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    expect(c.querySelector(".worker-chip")?.textContent).toBe("WORKER");
    const dot = c.querySelector(".thread-dot") as HTMLElement;
    expect(dot.style.background).toBe("rgb(10, 20, 30)");
    expect(c.querySelector(".timeline-toggle-label")?.textContent).toBe("sub-agent");
    const count = c.querySelector(".sub-agent-collapsed-count");
    expect(count?.textContent).toContain("1 event");
    // one renderable output event → preview present
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
    expect(c.querySelector(".timeline-block-body")).toBeNull();
  });

  it("pluralizes the collapsed count", () => {
    const c = renderTimeline({
      events: [outputEvent("a"), outputEvent("b")],
    });
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain(
      "2 events",
    );
  });

  it("renders the body and hides the preview when open", () => {
    const c = renderTimeline({ defaultOpen: true });
    const header = c.querySelector(".timeline-toggle-header")!;
    expect(header.getAttribute("aria-expanded")).toBe("true");
    expect(c.querySelector(".collapse-arrow")?.textContent).toBe("▼");
    expect(c.querySelector(".timeline-block-body")).not.toBeNull();
    expect(c.querySelector(".collapse-ellipsis")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).toBeNull();
  });

  it("expands on toggle click and keeps the body mounted after re-collapse", () => {
    const c = renderTimeline({ defaultOpen: false });
    const header = c.querySelector(".timeline-toggle-header")!;
    // open
    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("true");
    expect(c.querySelector(".timeline-block-body")).not.toBeNull();
    // close again → userToggled path, body gone, preview back
    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(c.querySelector(".timeline-block-body")).toBeNull();
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
  });

  it("applies the created class and anchor id when provided", () => {
    const c = renderTimeline({ created: true, anchorId: "w-42" });
    const block = c.querySelector(".collapsible-timeline-block") as HTMLElement;
    expect(block.classList.contains("timeline-block-created")).toBe(true);
    expect(block.id).toBe("w-42");
  });
});
