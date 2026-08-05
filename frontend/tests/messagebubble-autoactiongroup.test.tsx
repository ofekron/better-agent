import { describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, type RenderResult } from "@testing-library/react";
import { AutoActionGroup } from "../src/components/MessageBubble";
import type { WSEvent } from "../src/types";
import type { EventRenderGroup } from "../src/lib/groupEvents";

// AutoActionGroup is a presentational shell around already-grouped events.
// Its own contract is the collapse state machine, count/time chrome, and body
// mount/unmount — NOT the rendering of the lead or action bodies (those are
// exercised in their own slices via renderSingleEvent/renderTreeEntries). So
// the fixtures use `session_discovered` events, which renderSingleEvent maps
// to null, keeping this slice isolated to the component under test.
const nullEvent = (_ts?: string): WSEvent =>
  ({ type: "session_discovered", data: {}, _ts } as unknown as WSEvent);

const leadGroup = (idx: number, _ts?: string): EventRenderGroup => ({
  kind: "event",
  idx,
  event: nullEvent(_ts),
});

const actionGroups = (count: number): EventRenderGroup[] =>
  Array.from({ length: count }, (_, i) => ({ kind: "event", idx: 100 + i, event: nullEvent() }));

const renderGroup = (over: Partial<Parameters<typeof AutoActionGroup>[0]> & { actions?: EventRenderGroup[] } = {}): RenderResult => {
  const lead = over.lead ?? leadGroup(5);
  const actions = over.actions ?? actionGroups(2);
  return render(
    <AutoActionGroup
      lead={lead}
      actions={actions}
      childrenMap={over.childrenMap ?? new Map<string, WSEvent[]>()}
      defaultOpen={over.defaultOpen ?? true}
      nested={over.nested ?? false}
      {...over}
    />,
  );
};

const groupEl = (c: HTMLElement) => c.querySelector('[data-testid="auto-action-group"]')!;
const headerEl = (c: HTMLElement) => c.querySelector(".auto-action-group-header")!;
const bodyShell = (c: HTMLElement) => c.querySelector(".auto-action-group-body-shell");

describe("AutoActionGroup chrome", () => {
  it("renders the group container, collapse arrow, lead anchor, and action count", () => {
    const { container } = renderGroup({ actions: actionGroups(2) });
    const root = groupEl(container);
    expect(root).not.toBeNull();
    expect(root.className).toBe("auto-action-group open");
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    expect(container.querySelector(".auto-action-group-lead")?.id).toBe("action-lead-5");
    expect(container.querySelector(".auto-action-group-count")?.textContent).toBe("2 actions");
  });

  it("omits the time chip when the lead event has no timestamp", () => {
    const { container } = renderGroup({ lead: leadGroup(1) });
    expect(container.querySelector(".auto-action-group-time")).toBeNull();
  });

  it("shows a time chip when the lead event carries a timestamp", () => {
    const { container } = renderGroup({ lead: leadGroup(1, "2020-01-01T00:00:00Z") });
    const time = container.querySelector(".auto-action-group-time");
    expect(time).not.toBeNull();
    expect(time?.textContent?.trim().length).toBeGreaterThan(0);
  });
});

describe("AutoActionGroup count pluralization", () => {
  it("uses singular form for one action", () => {
    const { container } = renderGroup({ actions: actionGroups(1) });
    expect(container.querySelector(".auto-action-group-count")?.textContent).toBe("1 action");
  });

  it("uses plural form for multiple actions", () => {
    const { container } = renderGroup({ actions: actionGroups(3) });
    expect(container.querySelector(".auto-action-group-count")?.textContent).toBe("3 actions");
  });
});

describe("AutoActionGroup click toggle", () => {
  it("closes an open group and reflects aria-expanded", () => {
    const { container } = renderGroup({ defaultOpen: true });
    const root = groupEl(container);
    const header = headerEl(container);
    expect(root.className).toBe("auto-action-group open");
    expect(header.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(header);

    expect(root.className).toBe("auto-action-group");
    expect(header.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens a closed group", () => {
    const { container } = renderGroup({ defaultOpen: false });
    const root = groupEl(container);
    expect(root.className).toBe("auto-action-group");
    fireEvent.click(headerEl(container));
    expect(root.className).toBe("auto-action-group open");
  });
});

describe("AutoActionGroup keyboard toggle", () => {
  it("toggles on Enter and Space, ignores other keys", () => {
    const { container } = renderGroup({ defaultOpen: true });
    const header = headerEl(container);
    const root = groupEl(container);

    const keydown = (key: string) => fireEvent.keyDown(header, { key });

    keydown("ArrowDown");
    expect(root.className).toBe("auto-action-group open");

    keydown("Enter");
    expect(root.className).toBe("auto-action-group");

    keydown(" ");
    expect(root.className).toBe("auto-action-group open");
  });
});

describe("AutoActionGroup body mount lifecycle", () => {
  // happy-dom does not drive CSS transitions, so the body stays mounted until
  // a transitionend event arrives — mirroring the production unmount-on-close.
  it("mounts body when opened and unmounts it after a transitionend on close", () => {
    const { container } = renderGroup({ defaultOpen: false });
    // Closed from the start: body is not mounted.
    expect(bodyShell(container)).toBeNull();

    // Open -> useEffect mounts the body.
    fireEvent.click(headerEl(container));
    let shell = bodyShell(container);
    expect(shell).not.toBeNull();
    expect(shell?.getAttribute("aria-hidden")).toBe("false");

    // A transitionend while open must NOT unmount the body.
    fireEvent.transitionEnd(shell!);
    expect(bodyShell(container)).not.toBeNull();

    // Close, then transitionend unmounts the body shell.
    fireEvent.click(headerEl(container));
    shell = bodyShell(container);
    expect(shell?.getAttribute("aria-hidden")).toBe("true");
    fireEvent.transitionEnd(shell!);
    expect(bodyShell(container)).toBeNull();
  });

  it("mounts the body immediately when defaultOpen is true", () => {
    const { container } = renderGroup({ defaultOpen: true });
    expect(bodyShell(container)).not.toBeNull();
    cleanup();
  });
});
