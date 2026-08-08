// Native ForkSplitView — pane chrome (tabs, grid, focus/close/reopen
// controls) over N panes (root + user-facing forks), each pane's body a
// native ChatSurfaceView keyed by its own session id.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ForkSplitView } from "../../src/surface/ForkSplitView";
import { MockWebSocketController } from "../harness/mockWebSocket";
import { jsonResponse, snapshotEnvelope } from "./fixtures";
import type { Session } from "../../src/types";

function pane(id: string, overrides: Partial<Session> = {}): Session {
  return {
    id,
    name: id,
    model: "claude",
    cwd: "/repo",
    kind: "user",
    ...overrides,
  } as Session;
}

describe("surface/ForkSplitView", () => {
  let ws: MockWebSocketController;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    // Force desktop mode (useViewport.ts's BP_TABLET=1024 cutoff) so every
    // pane renders — this suite doesn't cover the mobile single-pane path.
    Object.defineProperty(window, "innerWidth", { value: 1400, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    ws = new MockWebSocketController();
    ws.install();
    fetchMock = vi.fn();
    // Every pane's own SurfaceStore hydrates independently by session id
    // (client.ts's fetchSnapshot) — this test only asserts the pane
    // CHROME, so every request resolves the same empty-but-hydrated
    // snapshot regardless of which session id it targets.
    fetchMock.mockImplementation(() => jsonResponse(snapshotEnvelope([])));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    ws.uninstall();
    vi.unstubAllGlobals();
  });

  it("renders one pane per root + fork, each with its own ChatSurfaceView", async () => {
    const root = pane("root", { forks: [pane("fork-a"), pane("fork-b")] });
    const onSetFocus = vi.fn();
    render(
      <ForkSplitView
        tree={root}
        focusedSessionId="root"
        onSetFocus={onSetFocus}
        onCloseFork={vi.fn()}
        onReopenFork={vi.fn()}
      />,
    );

    const panes = screen.getAllByTestId("fork-pane");
    expect(panes).toHaveLength(3);
    expect(panes.map((p) => p.getAttribute("data-session-id"))).toEqual(["root", "fork-a", "fork-b"]);

    await waitFor(() => {
      expect(screen.getAllByTestId("surface-chat-view").length).toBe(3);
    });
  });

  it("clicking a tab calls onSetFocus with that pane's session id", async () => {
    const root = pane("root", { forks: [pane("fork-a")] });
    const onSetFocus = vi.fn();
    render(
      <ForkSplitView
        tree={root}
        focusedSessionId="root"
        onSetFocus={onSetFocus}
        onCloseFork={vi.fn()}
        onReopenFork={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: "fork-a" }));
    expect(onSetFocus).toHaveBeenCalledWith("fork-a");
  });

  it("a closed non-root pane shows reopen (and delete, when provided) instead of the focus control", () => {
    const root = pane("root", { forks: [pane("fork-a", { fork_closed: true })] });
    const onReopenFork = vi.fn();
    const onDeleteFork = vi.fn();
    render(
      <ForkSplitView
        tree={root}
        focusedSessionId="root"
        onSetFocus={vi.fn()}
        onCloseFork={vi.fn()}
        onReopenFork={onReopenFork}
        onDeleteFork={onDeleteFork}
      />,
    );

    const closedPane = screen.getAllByTestId("fork-pane")[1];
    expect(closedPane.querySelector(".fork-pane-focus-radio")).toBeNull();
    expect(closedPane.querySelector(".fork-pane-reopen")).toBeTruthy();
    expect(closedPane.querySelector(".fork-pane-delete")).toBeTruthy();
  });

  it("no tab strip when there is only the root pane (single-pane tree)", () => {
    const root = pane("root");
    render(
      <ForkSplitView tree={root} focusedSessionId="root" onSetFocus={vi.fn()} onCloseFork={vi.fn()} onReopenFork={vi.fn()} />,
    );
    expect(screen.queryByTestId("fork-tabs-strip")).toBeNull();
    expect(screen.getAllByTestId("fork-pane")).toHaveLength(1);
  });
});
