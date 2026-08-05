import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { Fragment } from "react";
import {
  SubAgentBlock,
  jumpToParentEl,
  renderTreeEntry,
} from "../src/components/MessageBubble";
import type { EventRenderGroup } from "../src/lib/groupEvents";
import type { WSEvent } from "../src/types";

// This slice locks SubAgentBlock's collapsed/expanded state machine and the
// jumpToParentEl DOM helper.
//
// SubAgentBlock is reached in production only through renderTreeEntry, which
// guards `children.length > 0` and derives `defaultOpen = g.result === undefined`.
// Here we exercise the component directly so every branch of its internal state
// is covered: the collapsed-count pluralization, the lastEventPreview useMemo
// (open / empty-children / renderable-children paths), the open/close click and
// Enter/Space keydown toggles, and the expanded `.sub-agent-children` render.
//
// ToolCall is lazy (Suspense fallback null); assertions rest on the eager
// SubAgentBlock chrome (.sub-agent-header, .sub-agent-children,
// .sub-agent-collapsed-count, .collapse-ellipsis) and on toggle side-effects,
// never on suspended tool content.

const TS = "2026-01-01T10:00:00Z";

const ev = (e: Partial<WSEvent>) => e as WSEvent;

const toolEvent = (toolUseId: string): WSEvent =>
  ev({ type: "tool_call", data: { tool: "Task", tool_use_id: toolUseId, args: {} }, _ts: TS });

const renderBlock = (props: Partial<Parameters<typeof SubAgentBlock>[0]> = {}) => {
  const childEvents = props.childEvents ?? [
    ev({ type: "output", data: { output: "child work" } }),
  ];
  const { container } = render(
    <SubAgentBlock
      toolEvent={toolEvent("t1")}
      childEvents={childEvents}
      childrenMap={new Map<string, WSEvent[]>()}
      defaultOpen={false}
      {...props}
    />,
  );
  return container;
};

describe("SubAgentBlock expanded vs collapsed rendering", () => {
  it("renders the children body when open and hides the collapsed count", () => {
    const c = renderBlock({ defaultOpen: true });
    expect(c.querySelector(".sub-agent-children")).not.toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).toBeNull();
    expect(c.querySelector(".collapse-ellipsis")).toBeNull();
  });

  it("renders a collapsed count and last-event preview when closed with renderable children", () => {
    const c = renderBlock({
      defaultOpen: false,
      childEvents: [
        ev({ type: "output", data: { output: "first" } }),
        ev({ type: "output", data: { output: "second" } }),
      ],
    });
    expect(c.querySelector(".sub-agent-children")).toBeNull();
    // Plural form for 2 children.
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain("2 events");
    // lastEventPreview is truthy -> the ellipsis renders.
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
  });

  it("uses the singular form for exactly one child", () => {
    const c = renderBlock({
      defaultOpen: false,
      childEvents: [ev({ type: "output", data: { output: "only" } })],
    });
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain("1 event");
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).not.toContain("1 events");
  });

  it("omits the preview ellipsis when the last-event preview is null (empty children)", () => {
    // childCount === 0 short-circuits the preview useMemo to null, so the
    // `!open && lastEventPreview &&` block does not render. Defensive contract:
    // an empty SubAgentBlock stays intact with a "0 events" count and no body.
    const c = renderBlock({ defaultOpen: false, childEvents: [] });
    expect(c.querySelector(".collapse-ellipsis")).toBeNull();
    expect(c.querySelector(".sub-agent-children")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain("0 events");
  });

  it("omits the preview ellipsis when no child survives preview filtering", () => {
    // `complete` is in COLLAPSED_PREVIEW_NON_USER_FACING, so the preview is null
    // even though childCount > 0. Covers the falsy side of the preview conditional.
    const c = renderBlock({
      defaultOpen: false,
      childEvents: [ev({ type: "complete", data: {} })],
    });
    expect(c.querySelector(".collapse-ellipsis")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain("1 event");
  });
});

describe("SubAgentBlock open/close toggle", () => {
  it("expands on header click when collapsed", () => {
    const c = renderBlock({ defaultOpen: false });
    expect(c.querySelector(".sub-agent-children")).toBeNull();
    fireEvent.click(c.querySelector(".sub-agent-header")!);
    expect(c.querySelector(".sub-agent-children")).not.toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).toBeNull();
  });

  it("collapses on header click when expanded", () => {
    const c = renderBlock({ defaultOpen: true });
    fireEvent.click(c.querySelector(".sub-agent-header")!);
    expect(c.querySelector(".sub-agent-children")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).not.toBeNull();
  });

  it("toggles open on Enter keydown", () => {
    const c = renderBlock({ defaultOpen: false });
    fireEvent.keyDown(c.querySelector(".sub-agent-header")!, { key: "Enter" });
    expect(c.querySelector(".sub-agent-children")).not.toBeNull();
  });

  it("toggles closed on Space keydown", () => {
    const c = renderBlock({ defaultOpen: true });
    const header = c.querySelector(".sub-agent-header")!;
    fireEvent.keyDown(header, { key: " " });
    // Space matches the `||` right side -> handler runs (calling preventDefault
    // and toggling), closing the block.
    expect(c.querySelector(".sub-agent-children")).toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")).not.toBeNull();
  });
});

describe("jumpToParentEl DOM helper", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("scrolls the chat container into view and flashes the element, then removes the flash", () => {
    const container = document.createElement("div");
    container.className = "chat-messages";
    container.scrollTo = vi.fn();
    const target = document.createElement("div");
    container.appendChild(target);
    document.body.appendChild(container);

    jumpToParentEl(target);
    expect(container.scrollTo).toHaveBeenCalledOnce();
    expect(target.classList.contains("highlight-flash")).toBe(true);

    vi.advanceTimersByTime(1500);
    expect(target.classList.contains("highlight-flash")).toBe(false);

    container.remove();
  });

  it("still flashes the element when no scroll container is found", () => {
    // No `.chat-messages` / `.supervisor-timeline` ancestor -> the `if (container)`
    // branch is skipped but the highlight-flash is applied unconditionally.
    const orphan = document.createElement("div");
    document.body.appendChild(orphan);

    jumpToParentEl(orphan);
    expect(orphan.classList.contains("highlight-flash")).toBe(true);

    vi.advanceTimersByTime(1500);
    expect(orphan.classList.contains("highlight-flash")).toBe(false);

    orphan.remove();
  });
});

describe("renderLastEventPreview nested-tool + outerToolResultById branches", () => {
  // SubAgentBlock's collapsed preview (renderLastEventPreview) walks the
  // grouped pipeline and, when the last top-level group is a tool, recurses
  // into that tool's descendant events (includeDescendantEvents) before
  // falling back to the tool call itself. It also merges the caller-supplied
  // toolResultById (outerToolResultById) into the locally-flattened map so a
  // pre-flattened child stream — which carries no tool_result blocks of its
  // own — can still pair tool_call results from the parent's map.
  //
  // ToolCall is lazy (Suspense fallback null); a tool-call fallback preview
  // therefore renders wrapWithTs chrome + the Suspense wrapper (truthy, so the
  // ellipsis shows) but no tool content text. A descendant output preview
  // renders the descendant's actual text.

  const nestedToolChildEvents = (descendant: WSEvent) => [
    ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "inner", args: {} }, _ts: TS }),
    descendant,
  ];

  it("recurses into a nested tool's descendant and shows the descendant preview", () => {
    // partitionEventsByParent nests the output under the tool_call, so the only
    // top-level group is the tool; the tool branch recurses and the descendant
    // output becomes the preview.
    const c = renderBlock({
      defaultOpen: false,
      childEvents: nestedToolChildEvents(
        ev({
          type: "output",
          data: { output: "descendant preview text", parent_tool_use_id: "inner" },
          _ts: TS,
        }),
      ),
    });
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
    expect(c.textContent).toContain("descendant preview text");
  });

  it("falls back to the tool-call preview when the descendant recurses to null", () => {
    // The descendant is a `complete` event — filtered out by
    // COLLAPSED_PREVIEW_NON_USER_FACING — so the recursion returns null and the
    // tool branch falls through to wrapWithTs(ToolCall). ToolCall is suspended,
    // so the preview chrome (ellipsis) renders but no descendant text appears.
    const c = renderBlock({
      defaultOpen: false,
      childEvents: nestedToolChildEvents(
        ev({ type: "complete", data: { parent_tool_use_id: "inner" }, _ts: TS }),
      ),
    });
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
    expect(c.textContent).not.toContain("complete");
  });

  it("falls back to the tool-call preview when the nested tool has no child entry", () => {
    // A lone top-level tool_call with no descendant events: children.get() is
    // undefined, so the `childEvents && childEvents.length > 0` guard
    // short-circuits straight to the tool-call fallback.
    const c = renderBlock({
      defaultOpen: false,
      childEvents: [
        ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "inner", args: {} }, _ts: TS }),
      ],
    });
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
    expect(c.querySelector(".sub-agent-collapsed-count")?.textContent).toContain("1 event");
  });

  it("forwards the caller toolResultById into the preview pipeline (outerToolResultById merge)", () => {
    // SubAgentBlock forwards its toolResultById prop as renderLastEventPreview's
    // outerToolResultById. The merge loop runs (the flat child stream yields an
    // empty local map, so the outer key is new and is set), and the descendant
    // preview still renders. Locks the production config — every SubAgentBlock
    // receives toolResultById from renderTreeLevel — not the merge's internal
    // dedup branch, which is dead at this call site (the local map is always
    // empty for pre-flattened child streams).
    const toolResultById = new Map<string, string>([["unrelated", "result-text"]]);
    const c = renderBlock({
      defaultOpen: false,
      toolResultById,
      childEvents: [
        ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "inner", args: {} }, _ts: TS }),
        ev({
          type: "output",
          data: { output: "descendant via forwarded map", parent_tool_use_id: "inner" },
          _ts: TS,
        }),
      ],
    });
    expect(c.querySelector(".collapse-ellipsis")).not.toBeNull();
    expect(c.textContent).toContain("descendant via forwarded map");
  });
});

describe("wrapWithTs jump-to-parent button invokes jumpToParentEl", () => {
  it("clicking the jump button flashes the resolved parent element", () => {
    // renderTreeEntry wires parentId from parent_tool_use_id; the button's
    // onClick resolves document.getElementById(parentId) and calls jumpToParentEl.
    const parent = document.createElement("div");
    parent.id = "tu-p1";
    document.body.appendChild(parent);

    const g: EventRenderGroup = {
      kind: "event",
      idx: 0,
      event: ev({
        type: "output",
        data: { output: "nested", parent_tool_use_id: "p1" },
        _ts: TS,
      }),
    };
    const { container } = render(
      <Fragment>
        {renderTreeEntry(g, new Map(), undefined, undefined, false, undefined, "msg1")}
      </Fragment>,
    );

    fireEvent.click(container.querySelector(".jump-to-parent-btn")!);
    // jumpToParentEl applied the flash synchronously.
    expect(parent.classList.contains("highlight-flash")).toBe(true);

    parent.remove();
  });
});
