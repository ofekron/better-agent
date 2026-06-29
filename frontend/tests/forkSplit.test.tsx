import { beforeEach, describe, it, expect } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { ForkSplitView } from "../src/components/ForkSplitView";
import type { Session } from "../src/types";

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: 1280,
  });
  Object.defineProperty(window, "innerHeight", {
    configurable: true,
    writable: true,
    value: 800,
  });
});

/** Render a component into a detached container — used for unit tests
 * of ForkSplitView in isolation, without the full App harness. */
async function renderComponent(node: React.ReactNode): Promise<{
  container: HTMLElement;
  unmount: () => void;
  rerender: (n: React.ReactNode) => Promise<void>;
}> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = null;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  return {
    container,
    unmount: () => {
      act(() => root?.unmount());
      container.remove();
    },
    rerender: async (n: React.ReactNode) => {
      await act(async () => root?.render(n));
    },
  };
}

function makeFork(parent: Session, overrides: Partial<Session> = {}): Session {
  return {
    ...parent,
    id: overrides.id ?? `${parent.id}-fork`,
    name: overrides.name ?? `${parent.name} (fork)`,
    parent_session_id: parent.id,
    fork_point_seq: (parent.messages?.length ?? 1) - 1,
    fork_closed: overrides.fork_closed ?? false,
    forks: [],
    messages: overrides.messages ?? [],
    ...overrides,
  };
}

// ───────────────────────────── ForkSplitView (isolated) ─────────────

describe("ForkSplitView (isolated)", () => {
  it("renders shared messages above the split + N+1 columns below", async () => {
    const sharedUser = makeUserMsg({ id: "u1", content: "shared", seq: 0 });
    const sharedAsst = makeAssistantMsg({ id: "a1", content: "ok", seq: 1 });
    const root: Session = {
      ...makeSession({ id: "root", name: "main" }),
      messages: [sharedUser, sharedAsst],
      forks: [],
    };
    const fork1 = makeFork(root, {
      id: "fork-1",
      name: "fork one",
      messages: [
        sharedUser,
        sharedAsst,
        makeUserMsg({ id: "u2", content: "in fork 1", seq: 2 }),
      ],
    });
    const fork2 = makeFork(root, {
      id: "fork-2",
      name: "fork two",
      messages: [sharedUser, sharedAsst],
    });
    root.forks = [fork1, fork2];

    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={fork1.id}
        onSetFocus={() => {}}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
      />,
    );
    const panes = h.container.querySelectorAll('[data-testid="fork-pane"]');
    expect(panes.length).toBe(3); // root + fork1 + fork2
    // The shared region exists.
    expect(h.container.querySelector('[data-testid="fork-shared"]')).not.toBeNull();
    // Focused pane has the focused class.
    const focused = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${fork1.id}"]`,
    );
    expect(focused?.classList.contains("fork-pane-focused")).toBe(true);
    h.unmount();
  });

  it("focus radio click invokes onSetFocus with the pane id", async () => {
    const root: Session = {
      ...makeSession({ id: "root" }),
      messages: [makeUserMsg({ id: "u", seq: 0 })],
      forks: [],
    };
    const f = makeFork(root, { id: "f1" });
    root.forks = [f];

    const calls: string[] = [];
    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={root.id}
        onSetFocus={(id) => calls.push(id)}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
      />,
    );
    const fPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${f.id}"]`,
    );
    const radio = fPane?.querySelector(".fork-pane-focus-radio") as HTMLButtonElement;
    expect(radio).not.toBeNull();
    await act(async () => radio!.click());
    expect(calls).toEqual([f.id]);
    h.unmount();
  });

  it("opens one pane in focused view and returns to split view", async () => {
    const sharedUser = makeUserMsg({ id: "u1", content: "shared", seq: 0 });
    const root: Session = {
      ...makeSession({ id: "root", name: "main" }),
      messages: [sharedUser],
      forks: [],
    };
    const fork1 = makeFork(root, { id: "fork-1", name: "fork one" });
    const fork2 = makeFork(root, { id: "fork-2", name: "fork two" });
    root.forks = [fork1, fork2];

    const focusCalls: string[] = [];
    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={root.id}
        onSetFocus={(id) => focusCalls.push(id)}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
      />,
    );

    const forkPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${fork1.id}"]`,
    );
    const openButton = forkPane?.querySelector(".fork-pane-view-button") as HTMLButtonElement;
    await act(async () => openButton.click());

    expect(focusCalls).toEqual([fork1.id]);
    expect(h.container.querySelectorAll('[data-testid="fork-pane"]').length).toBe(1);
    expect(
      h.container.querySelector(`[data-testid="fork-pane"][data-session-id="${fork1.id}"]`),
    ).not.toBeNull();
    expect(h.container.querySelector('[data-testid="fork-focus-toolbar"]')).not.toBeNull();

    const back = h.container.querySelector('[data-testid="fork-back-to-split"]') as HTMLButtonElement;
    await act(async () => back.click());

    expect(h.container.querySelectorAll('[data-testid="fork-pane"]').length).toBe(3);
    expect(h.container.querySelector('[data-testid="fork-focus-toolbar"]')).toBeNull();
    h.unmount();
  });

  it("delete button on a closed fork prompts then fires onDeleteFork", async () => {
    const root: Session = {
      ...makeSession({ id: "root" }),
      messages: [makeUserMsg({ id: "u", seq: 0 })],
      forks: [],
    };
    const closed = makeFork(root, { id: "f-closed", name: "doomed", fork_closed: true });
    root.forks = [closed];

    const deleteCalls: string[] = [];
    const origConfirm = window.confirm;
    window.confirm = () => true;
    try {
      const h = await renderComponent(
        <ForkSplitView
          tree={root}
          pendingBySession={{}}
          runStateBySession={{}}
          focusedSessionId={root.id}
          onSetFocus={() => {}}
          onCloseFork={() => {}}
          onReopenFork={() => {}}
          onDeleteFork={(id) => deleteCalls.push(id)}
        />,
      );
      const closedPane = h.container.querySelector(
        `[data-testid="fork-pane"][data-session-id="${closed.id}"]`,
      );
      const delBtn = closedPane?.querySelector(".fork-pane-delete") as HTMLButtonElement;
      expect(delBtn).not.toBeNull();
      await act(async () => delBtn.click());
      expect(deleteCalls).toEqual([closed.id]);
      h.unmount();
    } finally {
      window.confirm = origConfirm;
    }
  });

  it("delete button absent on open forks (must close first)", async () => {
    const root: Session = {
      ...makeSession({ id: "root" }),
      messages: [makeUserMsg({ id: "u", seq: 0 })],
      forks: [],
    };
    const open = makeFork(root, { id: "f-open", fork_closed: false });
    root.forks = [open];

    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={root.id}
        onSetFocus={() => {}}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
        onDeleteFork={() => {}}
      />,
    );
    const openPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${open.id}"]`,
    );
    expect(openPane?.querySelector(".fork-pane-delete")).toBeNull();
    h.unmount();
  });

  it("close button shows for non-root, non-closed forks; reopen replaces it when closed", async () => {
    const root: Session = {
      ...makeSession({ id: "root" }),
      messages: [makeUserMsg({ id: "u", seq: 0 })],
      forks: [],
    };
    const open = makeFork(root, { id: "f-open", fork_closed: false });
    const closed = makeFork(root, { id: "f-closed", fork_closed: true });
    root.forks = [open, closed];

    const closeCalls: string[] = [];
    const reopenCalls: string[] = [];
    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={root.id}
        onSetFocus={() => {}}
        onCloseFork={(id) => closeCalls.push(id)}
        onReopenFork={(id) => reopenCalls.push(id)}
      />,
    );
    const openPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${open.id}"]`,
    );
    const closedPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="${closed.id}"]`,
    );

    // Open fork: close button present, reopen NOT present, has focus radio.
    expect(openPane?.querySelector(".fork-pane-close")).not.toBeNull();
    expect(openPane?.querySelector(".fork-pane-reopen")).toBeNull();
    expect(openPane?.querySelector(".fork-pane-focus-radio")).not.toBeNull();

    // Closed fork: close button NOT present, reopen IS present, no focus radio,
    // pane has the closed class.
    expect(closedPane?.querySelector(".fork-pane-close")).toBeNull();
    expect(closedPane?.querySelector(".fork-pane-reopen")).not.toBeNull();
    expect(closedPane?.querySelector(".fork-pane-focus-radio")).toBeNull();
    expect(closedPane?.classList.contains("fork-pane-closed")).toBe(true);

    // Click close on the open fork.
    const closeBtn = openPane!.querySelector(".fork-pane-close") as HTMLButtonElement;
    await act(async () => closeBtn.click());
    expect(closeCalls).toEqual([open.id]);

    // Click reopen on the closed fork.
    const reopenBtn = closedPane!.querySelector(".fork-pane-reopen") as HTMLButtonElement;
    await act(async () => reopenBtn.click());
    expect(reopenCalls).toEqual([closed.id]);
    h.unmount();
  });

  it("flattens nested forks into a single row of columns + uses earliest fork_point_seq for the split", async () => {
    // Tree:  root
    //         └── A (fork_point=0)
    //              └── B (fork_point=2)   ← nested fork of A
    // The split should sit at seq <= 0 (earliest fork point), so
    // even root's seq=1 message lives in a pane, not the shared region.
    const m0 = makeUserMsg({ id: "u0", content: "ROOT_BEFORE_FORK", seq: 0 });
    const m1 = makeAssistantMsg({ id: "a0", content: "ROOT_AFTER_FORK", seq: 1 });
    const root: Session = {
      ...makeSession({ id: "root" }),
      messages: [m0, m1],
      forks: [],
    };
    const aMsgs = [
      m0, // shared prefix
      makeUserMsg({ id: "ua1", content: "A_DIVERGENT", seq: 1 }),
      makeUserMsg({ id: "ua2", content: "A_BEFORE_B", seq: 2 }),
    ];
    const aFork: Session = {
      ...makeFork(root, { id: "A", fork_point_seq: 0 }),
      messages: aMsgs,
      forks: [],
    };
    const bMsgs = [...aMsgs, makeUserMsg({ id: "ub", content: "B_DIVERGENT", seq: 3 })];
    const bFork: Session = {
      ...makeFork(aFork, { id: "B", fork_point_seq: 2 }),
      messages: bMsgs,
      forks: [],
    };
    aFork.forks = [bFork];
    root.forks = [aFork];

    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={bFork.id}
        onSetFocus={() => {}}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
      />,
    );
    // Three columns: root, A, B (flattened depth-first).
    const panes = h.container.querySelectorAll('[data-testid="fork-pane"]');
    expect(panes.length).toBe(3);
    // Shared region holds the message before the EARLIEST fork point
    // (seq=0 — A's fork point). Root's seq=1 must NOT be in shared
    // since A diverged before then.
    const shared = h.container.querySelector('[data-testid="fork-shared"]');
    expect(shared?.textContent ?? "").toContain("ROOT_BEFORE_FORK");
    expect(shared?.textContent ?? "").not.toContain("ROOT_AFTER_FORK");
    // The B pane shows everything past seq=0 — A's divergence + B's.
    const bPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="B"]`,
    );
    expect(bPane?.textContent ?? "").toContain("A_DIVERGENT");
    expect(bPane?.textContent ?? "").toContain("B_DIVERGENT");
    h.unmount();
  });

  it("split slices messages by fork_point_seq — shared above, divergent below", async () => {
    // Shared: seq 0..1 ; divergent in fork: seq 2.
    const m0 = makeUserMsg({ id: "u0", content: "S0", seq: 0 });
    const m1 = makeAssistantMsg({ id: "a0", content: "S1", seq: 1 });
    const div = makeUserMsg({ id: "u2", content: "DIVERGENT", seq: 2 });
    const root: Session = {
      ...makeSession({ id: "r", name: "main" }),
      messages: [m0, m1],
      forks: [],
    };
    const fork: Session = {
      ...makeFork(root, { id: "f1", fork_point_seq: 1 }),
      // fork's messages include the shared prefix + divergent tail
      messages: [m0, m1, div],
    };
    root.forks = [fork];

    const h = await renderComponent(
      <ForkSplitView
        tree={root}
        pendingBySession={{}}
        runStateBySession={{}}
        focusedSessionId={root.id}
        onSetFocus={() => {}}
        onCloseFork={() => {}}
        onReopenFork={() => {}}
      />,
    );
    const shared = h.container.querySelector('[data-testid="fork-shared"]');
    const fPane = h.container.querySelector(
      `[data-testid="fork-pane"][data-session-id="f1"]`,
    );
    const sharedText = shared?.textContent ?? "";
    const fPaneText = fPane?.textContent ?? "";
    // Shared region renders S0/S1 but NOT the divergent message.
    expect(sharedText).toContain("S0");
    expect(sharedText).toContain("S1");
    expect(sharedText).not.toContain("DIVERGENT");
    // The fork pane renders the divergent message but NOT the shared
    // ones (they live in the shared region above).
    expect(fPaneText).toContain("DIVERGENT");
    expect(fPaneText).not.toContain("S0");
    h.unmount();
  });
});

// ─────────────────────────── App harness — fork flow ──────────────────

describe("fork-and-send through App harness", () => {
  it("session_forked WS event appends a new pane and auto-focuses it", async () => {
    const session = makeSession({
      id: "sess-A",
      manager_claude_session_id: "claude-A",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Server-side fork creation broadcasts session_forked.
    const child: Session = {
      ...session,
      id: "child-1",
      name: `${session.name} (fork)`,
      parent_session_id: session.id,
      fork_point_seq: 0,
      fork_closed: false,
      forks: [],
      messages: [],
    };
    h.emit({
      type: "session_forked",
      data: { session: child, parent_session_id: session.id },
    });
    await h.flush();

    // Both panes render.
    const panes = h.$$('[data-testid="fork-pane"]');
    expect(panes.length).toBe(2);

    // The fork is in the DOM under its session id.
    expect(
      h.$(`[data-testid="fork-pane"][data-session-id="${child.id}"]`),
    ).not.toBeNull();
    h.unmount();
  });

  it("close_fork updates the pane and falls focus through to the next open pane", async () => {
    const session = makeSession({
      id: "sess-A",
      manager_claude_session_id: "claude-A",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Spawn a fork via the WS (mirrors what fork_and_send does).
    const child: Session = {
      ...session,
      id: "child-1",
      name: `${session.name} (fork)`,
      parent_session_id: session.id,
      fork_point_seq: 0,
      fork_closed: false,
      forks: [],
      messages: [],
    };
    h.emit({
      type: "session_forked",
      data: { session: child, parent_session_id: session.id },
    });
    await h.flush();

    // Backend echoes a metadata patch flagging the fork as closed.
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: child.id,
        patch: { fork_closed: true },
        originated_by: null,
      },
    });
    await h.flush();

    const closedPane = h.$(
      `[data-testid="fork-pane"][data-session-id="${child.id}"]`,
    );
    expect(closedPane?.classList.contains("fork-pane-closed")).toBe(true);
    // Focus must have fallen back to the root (the only still-open
    // pane). The InputArea's fork-target chip should say "original".
    const chip = h.$('[data-testid="input-fork-target"]');
    expect(chip?.textContent ?? "").toContain("original");
    h.unmount();
  });
});

// ─────────────────────────── multi-WS subscribe ──────────────────────

describe("multi-pane WebSocket subscription", () => {
  it("subscribes to a new fork id when session_forked arrives, unsubscribes when the tree changes", async () => {
    const root = makeSession({
      id: "root",
      manager_claude_session_id: "claude-A",
    });
    const other = makeSession({ id: "other" });
    const h = await renderApp({ seed: { sessions: [root, other] } });
    await h.selectSession(root.id);

    const subFor = (sid: string) =>
      h.outbound.find(
        (f) =>
          (f as { type?: string; app_session_id?: string }).type ===
            "subscribe" &&
          (f as { type?: string; app_session_id?: string }).app_session_id ===
            sid,
      );
    const unsubFor = (sid: string) =>
      h.outbound.find(
        (f) =>
          (f as { type?: string; app_session_id?: string }).type ===
            "unsubscribe" &&
          (f as { type?: string; app_session_id?: string }).app_session_id ===
            sid,
      );

    // Root is subscribed on select.
    expect(subFor(root.id)).toBeDefined();

    // Fork is born — frontend must subscribe to it.
    const child: Session = {
      ...root,
      id: "fork-1",
      parent_session_id: root.id,
      fork_point_seq: 0,
      fork_closed: false,
      forks: [],
      messages: [],
    };
    h.emit({
      type: "session_forked",
      data: { session: child, parent_session_id: root.id },
    });
    await h.flush();
    expect(subFor(child.id)).toBeDefined();

    // Now switch sessions away — must unsubscribe BOTH ids.
    await h.selectSession(other.id);
    expect(unsubFor(root.id)).toBeDefined();
    expect(unsubFor(child.id)).toBeDefined();
    h.unmount();
  });
});

// ─────────────────────────── useSession reducers ──────────────────────

describe("useSession tree reducers (via WS replay routing)", () => {
  it("messages_replay routed to a fork id lands in the fork's slot, not the root's", async () => {
    const root = makeSession({
      id: "root",
      manager_claude_session_id: "claude-A",
    });
    const h = await renderApp({ seed: { sessions: [root] } });
    await h.selectSession(root.id);

    const child: Session = {
      ...root,
      id: "fork-A",
      parent_session_id: root.id,
      fork_point_seq: 0,
      fork_closed: false,
      forks: [],
      messages: [],
    };
    h.emit({
      type: "session_forked",
      data: { session: child, parent_session_id: root.id },
    });
    await h.flush();

    const userMsg = makeUserMsg({ id: "u-fork", content: "FORK MSG", seq: 1 });
    const asstMsg = makeAssistantMsg({ id: "a-fork", content: "FORK REPLY", seq: 2 });
    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: child.id,
        messages: [userMsg, asstMsg],
      },
    });
    await h.flush();

    // The fork pane should contain the fork-only messages.
    const forkPane = h.$(
      `[data-testid="fork-pane"][data-session-id="${child.id}"]`,
    );
    const text = forkPane?.textContent ?? "";
    expect(text).toContain("FORK MSG");
    expect(text).toContain("FORK REPLY");

    // The ROOT pane should NOT have absorbed those messages.
    const rootPane = h.$(
      `[data-testid="fork-pane"][data-session-id="${root.id}"]`,
    );
    const rootText = rootPane?.textContent ?? "";
    expect(rootText).not.toContain("FORK MSG");
    expect(rootText).not.toContain("FORK REPLY");
    h.unmount();
  });
});
