import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Fragment } from "react";
import {
  renderGroupedEvents,
  renderTreeEntry,
} from "../src/components/MessageBubble";
import type { EventRenderGroup } from "../src/lib/groupEvents";
import type { WSEvent } from "../src/types";

// This slice locks the event-grouping pipeline: renderGroupedEvents ->
// renderTreeLevel -> renderTreeEntry -> wrapWithTs, plus the AutoActionGroup /
// SubAgentBlock routing decisions (isActionLeadGroup, isAutoGroupedAction,
// isSubAgentAction, isHeadlessLead, hasLaterActionLead). It exercises every
// branch of renderTreeLevel's lead-scanning loop and renderTreeEntry's
// tool-with-children vs tool-without-children vs event split.
//
// ToolCall is lazy (Suspense fallback null); assertions rest on the stable
// wrapper chrome (.timeline-event-row, ids, .timeline-event-time) and the
// eager AutoActionGroup/SubAgentBlock containers, never on suspended content.

const TS = "2026-01-01T10:00:00Z";

const ev = (e: Partial<WSEvent>) => e as WSEvent;

const grouped = (events: WSEvent[]) =>
  render(<Fragment>{renderGroupedEvents(events)}</Fragment>).container;

describe("renderGroupedEvents action-lead grouping", () => {
  it("renders a lone lead with no following actions as a plain row (no auto group)", () => {
    // Lead is an action lead (output), but the forward scan finds no actions,
    // so actions.length === 0 -> renderTreeEntry(lead), no AutoActionGroup.
    const c = grouped([ev({ type: "output", data: { output: "just text" } })]);
    expect(c.querySelector('[data-testid="auto-action-group"]')).toBeNull();
    expect(c.textContent).toContain("just text");
  });

  it("wraps a lead followed by tool actions in an AutoActionGroup", () => {
    const c = grouped([
      ev({ type: "output", data: { output: "Running" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "a", args: {} } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "b", args: {} } }),
    ]);
    const group = c.querySelector('[data-testid="auto-action-group"]');
    expect(group).not.toBeNull();
    // <= AUTO_ACTION_OPEN_MAX (3) and no later lead -> open by default.
    expect(group?.classList.contains("open")).toBe(true);
  });

  // `thinking` is used for leads that must survive after a tool: groupEvents'
  // legacy positional fallback consumes an `output` immediately following a
  // tool_call as that tool's result, but only `output` — `thinking` is never
  // positionally paired, so it remains its own group and acts as a real lead.

  it("breaks the action run at a non-headless second lead (two separate groups)", () => {
    const c = grouped([
      ev({ type: "output", data: { output: "First" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "a", args: {} } }),
      ev({ type: "thinking", data: { thought: "Second" } }),
    ]);
    // First lead + tool -> one AutoActionGroup; second lead renders standalone.
    expect(c.querySelectorAll('[data-testid="auto-action-group"]').length).toBe(1);
    expect(c.textContent).toContain("Second");
  });

  it("swallows a headless (empty-text) lead between actions into the running group", () => {
    const c = grouped([
      ev({ type: "output", data: { output: "Running" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "a", args: {} } }),
      ev({ type: "thinking", data: { thought: "" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "b", args: {} } }),
    ]);
    // The empty-thought lead is headless -> merged into the running group
    // instead of breaking it into two groups.
    expect(c.querySelectorAll('[data-testid="auto-action-group"]').length).toBe(1);
  });

  it("collapses a group larger than AUTO_ACTION_OPEN_MAX when there is no later lead", () => {
    const c = grouped([
      ev({ type: "output", data: { output: "Running" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "a", args: {} } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "b", args: {} } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "c", args: {} } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "d", args: {} } }),
    ]);
    const group = c.querySelector('[data-testid="auto-action-group"]');
    // 4 actions > AUTO_ACTION_OPEN_MAX (3) -> closed.
    expect(group?.classList.contains("open")).toBe(false);
  });

  it("collapses a group when a later action lead follows", () => {
    const c = grouped([
      ev({ type: "output", data: { output: "Running" } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "a", args: {} } }),
      ev({ type: "tool_call", data: { tool: "Bash", tool_use_id: "b", args: {} } }),
      ev({ type: "thinking", data: { thought: "Later lead" } }),
    ]);
    const group = c.querySelector('[data-testid="auto-action-group"]');
    // hasLaterActionLead -> closed.
    expect(group?.classList.contains("open")).toBe(false);
  });
});

describe("renderGroupedEvents sub-agent + standalone tool routing", () => {
  it("renders a tool with child events as a SubAgentBlock (not inside an auto group)", () => {
    // The sub-agent tool is an action but isSubAgentAction -> the lead's action
    // run breaks immediately (actions.length 0), so the lead renders alone and
    // the tool renders as a SubAgentBlock via its own renderTreeEntry.
    const c = grouped([
      ev({ type: "output", data: { output: "Delegating" } }),
      ev({ type: "tool_call", data: { tool: "Task", tool_use_id: "t1", args: {} } }),
      ev({ type: "output", data: { output: "child work", parent_tool_use_id: "t1" } }),
    ]);
    expect(c.querySelector(".sub-agent-block")).not.toBeNull();
    expect(c.querySelector('[data-testid="auto-action-group"]')).toBeNull();
    expect(c.textContent).toContain("Delegating");
  });

  it("renders a standalone tool_call with its tool_use_id as the row target id and a timestamp", () => {
    const c = grouped([
      ev({
        type: "tool_call",
        data: { tool: "Bash", tool_use_id: "t2", args: {} },
        _ts: TS,
      }),
    ]);
    // Non-action-lead -> renderTreeEntry tool branch, no children -> ToolCall
    // wrapper. wrapWithTs stamps id="tu-<tool_use_id>" and the time span.
    expect(c.querySelector("#tu-t2")).not.toBeNull();
    expect(c.querySelector(".timeline-event-time")).not.toBeNull();
  });
});

describe("renderTreeEntry direct routing", () => {
  const renderEntry = (
    g: EventRenderGroup,
    children: Map<string, WSEvent[]> = new Map(),
    parentMessageId?: string,
  ) =>
    render(
      <Fragment>
        {renderTreeEntry(g, children, undefined, undefined, false, undefined, parentMessageId)}
      </Fragment>,
    ).container;

  it("renders a tool group with children as a SubAgentBlock", () => {
    const g: EventRenderGroup = {
      kind: "tool",
      idx: 0,
      event: ev({ type: "tool_call", data: { tool: "Task", tool_use_id: "t1", args: {} } }),
    };
    const children = new Map([
      ["t1", [ev({ type: "output", data: { output: "child work" } })]],
    ]);
    const c = renderEntry(g, children);
    expect(c.querySelector(".sub-agent-block")).not.toBeNull();
    expect(c.textContent).toContain("child work");
  });

  it("renders a tool group without children as a ToolCall wrapper row with the tool_use_id target", () => {
    const g: EventRenderGroup = {
      kind: "tool",
      idx: 0,
      event: ev({
        type: "tool_call",
        data: { tool: "Bash", tool_use_id: "t9", args: {} },
        _ts: TS,
      }),
    };
    const c = renderEntry(g);
    expect(c.querySelector("#tu-t9")).not.toBeNull();
    expect(c.querySelector(".timeline-event-time")).not.toBeNull();
  });

  it("renders a tool group with no tool_use_id as a plain ToolCall (no children lookup, no target id)", () => {
    // Covers the falsy branch of `toolUseId ? childrenMap.get(toolUseId) : undefined`.
    const g: EventRenderGroup = {
      kind: "tool",
      idx: 0,
      event: ev({ type: "tool_call", data: { tool: "Bash", args: {} }, _ts: TS }),
    };
    const c = renderEntry(g);
    // No children -> ToolCall path (not a SubAgentBlock); no tool_use_id ->
    // no target id on the row, but the timestamp still renders.
    expect(c.querySelector(".sub-agent-block")).toBeNull();
    expect(c.querySelector(".timeline-event-time")).not.toBeNull();
  });

  it("renders an event group through renderSingleEvent", () => {
    const g: EventRenderGroup = {
      kind: "event",
      idx: 0,
      event: ev({ type: "output", data: { output: "hello" } }),
    };
    const c = renderEntry(g);
    expect(c.textContent).toContain("hello");
  });

  it("adds a jump-to-parent button keyed off parent_tool_use_id", () => {
    const g: EventRenderGroup = {
      kind: "event",
      idx: 0,
      event: ev({ type: "output", data: { output: "nested", parent_tool_use_id: "p1" } }),
    };
    const c = renderEntry(g, new Map(), "msg1");
    expect(c.querySelector(".jump-to-parent-btn")).not.toBeNull();
  });

  it("falls back to the parentMessageId for the jump-to-parent target", () => {
    const g: EventRenderGroup = {
      kind: "event",
      idx: 0,
      event: ev({ type: "output", data: { output: "nested" } }),
    };
    const c = renderEntry(g, new Map(), "m9");
    expect(c.querySelector(".jump-to-parent-btn")).not.toBeNull();
  });
});
