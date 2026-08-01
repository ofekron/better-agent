/* Dedicated coverage for ForkSplitView. TurnGroup (MessageBubble) and the
 * viewport/progress/scroll/provider/thread helpers are stubbed so the test
 * exercises ForkSplitView's own logic: tree flattening, fork-point split,
 * pane/tab/focus rendering, focused-view toggle, close/reopen/delete,
 * load-older states, mobile single-pane, run anchoring, and the tab-strip
 * touch-swipe handler. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { ForkSplitView } from "../src/components/ForkSplitView";
import type { Session, ChatMessage, RunInfo } from "../src/types";

// --- stable i18n (component memoizes paneLabel on [t]; stable ref required) ---
vi.mock("react-i18next", () => {
  const t = (key: string, opts?: unknown) => {
    if (typeof opts === "string") return opts;
    let s = key;
    if (opts && typeof opts === "object") {
      for (const [k, v] of Object.entries(opts as Record<string, unknown>)) {
        s = s.replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
      }
    }
    return s;
  };
  return { useTranslation: () => ({ t, i18n: { language: "en" } }) };
});

// --- control plane for the stubbed hooks ---
const control = vi.hoisted(() => ({
  viewportMode: "desktop" as "desktop" | "mobile" | "tablet",
  opInflight: false,
  scrollLoadError: null as string | null,
  triggers: [] as string[],
  // stable per-opId ref objects so React actually attaches them to the DOM,
  // letting the pane auto-scroll effect read a real element.
  refs: new Map<string, { current: HTMLElement | null }>(),
}));

vi.mock("../src/components/MessageBubble", () => ({
  TurnGroup: (props: ComponentProps<typeof import("../src/components/MessageBubble").TurnGroup>) => (
    <div
      data-testid="tg"
      data-init={props.initiatorMessage.id}
      data-resp={props.responseMessage?.id ?? ""}
      data-runs={props.runs?.length ?? 0}
      data-sess={props.sessionId ?? ""}
    />
  ),
}));

vi.mock("../src/hooks/useViewport", () => ({
  useViewport: () => ({ width: 1200, height: 800, mode: control.viewportMode }),
}));

vi.mock("../src/progress/store", () => ({
  useOpProgress: () => ({ inflight: control.opInflight, error: null }),
}));

vi.mock("../src/hooks/useScrollLoadOlder", () => ({
  // Thin stub: mirrors the real contract (trigger fires the supplied
  // loader) so the closure ForkSplitView builds per-pane is exercised.
  // Returns a STABLE per-opId ref so React attaches it to the DOM.
  useScrollLoadOlder: (
    opId: string,
    _hasOlder: boolean,
    onLoadOlder: (() => Promise<boolean | void>) | undefined,
  ) => {
    let ref = control.refs.get(opId);
    if (!ref) {
      ref = { current: null };
      control.refs.set(opId, ref);
    }
    return {
      scrollRef: ref,
      handleScroll: () => {},
      triggerLoadOlder: () => {
        control.triggers.push(opId);
        if (typeof onLoadOlder === "function") return onLoadOlder();
      },
      loadError: control.scrollLoadError,
    };
  },
}));

vi.mock("../src/threadColors", () => ({
  buildThreadColorMap: (ids: string[]) =>
    new Map(ids.map((id, i) => [id, `c${i}`])),
}));

vi.mock("../src/utils/providerCache", () => ({
  providerNameForId: (id?: string | null) => (id ? `Prov-${id}` : ""),
}));

// mergeMessagesSorted + isUnanchoredRun stay REAL (pure logic under test).

// --- fixtures ---
function msg(id: string, role: "user" | "assistant", s: number): ChatMessage {
  return { id, role, seq: s, timestamp: String(s), content: id };
}

/** root carries the full shared history; a fork at fork_point_seq=2 keeps
 * the same history (same seqs) and adds one extra user turn so its
 * divergent pane is non-empty. */
function buildTree(overrides: Partial<Session> = {}): Session {
  const root: Session = {
    id: "root",
    name: "Root",
    kind: "user",
    messages: [msg("u1", "user", 1), msg("a1", "assistant", 2), msg("u2", "user", 3), msg("a2", "assistant", 4)],
    forks: [],
    ...overrides,
  };
  return root;
}

function forkedTree(): Session {
  const shared = [msg("u1", "user", 1), msg("a1", "assistant", 2), msg("u2", "user", 3), msg("a2", "assistant", 4)];
  const root: Session = { id: "root", name: "Root", kind: "user", messages: [...shared], forks: [] };
  const f1: Session = {
    id: "f1",
    name: "feature-x",
    kind: "user",
    fork_point_seq: 2,
    messages: [...shared, msg("u3", "user", 5)],
    forks: [],
  };
  root.forks = [f1];
  return root;
}

function renderView(overrides: Partial<Parameters<typeof ForkSplitView>[0]> = {}) {
  const onSetFocus = vi.fn();
  const onCloseFork = vi.fn();
  const onReopenFork = vi.fn();
  const onDeleteFork = vi.fn();
  const tree = overrides.tree ?? forkedTree();
  const props: ComponentProps<typeof ForkSplitView> = {
    tree,
    pendingBySession: {},
    runStateBySession: {},
    focusedSessionId: "root",
    onSetFocus,
    onCloseFork,
    onReopenFork,
    onDeleteFork,
    ...overrides,
    tree,
  };
  const utils = render(<ForkSplitView {...props} />);
  return { ...utils, onSetFocus, onCloseFork, onReopenFork, onDeleteFork };
}

describe("ForkSplitView", () => {
  beforeEach(() => {
    control.viewportMode = "desktop";
    control.opInflight = false;
    control.scrollLoadError = null;
    control.triggers = [];
    control.refs.clear();
    document.documentElement.removeAttribute("dir");
  });

  describe("tree flattening + fork-point split", () => {
    it("flattens root + forks depth-first and skips non-user kind", () => {
      const root = buildTree();
      const internalFork: Session = { id: "internal", kind: "internal", fork_point_seq: 2, messages: [] };
      const userFork: Session = { id: "f1", kind: "user", fork_point_seq: 2, messages: [] };
      root.forks = [internalFork, userFork];
      renderView({ tree: root });
      // internal skipped → only root + f1 panes
      expect(screen.getAllByTestId("fork-pane").map((p) => p.dataset.sessionId)).toEqual([
        "root",
        "f1",
      ]);
    });

    it("splits shared (seq<=forkPoint) from per-pane divergent (seq>forkPoint)", () => {
      renderView();
      const shared = screen.getByTestId("fork-shared");
      // shared holds the pre-fork turns u1,a1 → one user/assistant group (init u1)
      expect(within(shared).getAllByTestId("tg").map((t) => t.dataset.init)).toEqual(["u1"]);
      // root pane shows the divergence u2,a2; f1 pane shows u2,a2,u3
      const panes = screen.getAllByTestId("fork-pane");
      const rootPane = panes.find((p) => p.dataset.sessionId === "root")!;
      const f1Pane = panes.find((p) => p.dataset.sessionId === "f1")!;
      // u1 (user) pairs with a1 (assistant); u2 (user) alone → two groups
      expect(within(rootPane).getAllByTestId("tg").map((t) => t.dataset.init)).toEqual(["u2"]);
      expect(within(f1Pane).getAllByTestId("tg").map((t) => t.dataset.init)).toEqual(["u2", "u3"]);
    });

    it("forkPointSeq null (no forks) shares all + renders all in pane", () => {
      const root = buildTree();
      renderView({ tree: root });
      const shared = screen.getByTestId("fork-shared");
      // u1→a1 pair, u2→a2 pair → two groups, init u1 and u2
      expect(within(shared).getAllByTestId("tg").map((t) => t.dataset.init)).toEqual(["u1", "u2"]);
    });

    it("nested forks flatten depth-first after their parent", () => {
      const root = buildTree();
      const child: Session = { id: "child", kind: "user", fork_point_seq: 2, messages: [] };
      const parent: Session = { id: "parent", kind: "user", fork_point_seq: 2, messages: [], forks: [child] };
      root.forks = [parent];
      renderView({ tree: root });
      expect(screen.getAllByTestId("fork-pane").map((p) => p.dataset.sessionId)).toEqual([
        "root",
        "parent",
        "child",
      ]);
    });

    it("builds a thread-color map from the tree's workers", () => {
      const root = forkedTree();
      // non-empty workers exercises the worker→id map (and filters falsy ids)
      root.workers = [{ agent_session_id: "w1" }, { agent_session_id: undefined as unknown as string }, { agent_session_id: "w2" }];
      renderView({ tree: root });
      // both panes still render with the worker-derived color map in place
      expect(screen.getAllByTestId("fork-pane").map((p) => p.dataset.sessionId)).toEqual([
        "root",
        "f1",
      ]);
    });
  });

  describe("pane labels + meta", () => {
    it("labels root as original, fork by name, and joins provider/model/effort meta", () => {
      const root = buildTree();
      root.provider_id = "provA";
      root.model = "sonnet";
      root.reasoning_effort = "high";
      const f1: Session = { id: "f1", name: "", kind: "user", fork_point_seq: 2, messages: [] };
      root.forks = [f1];
      renderView({ tree: root });
      const panes = screen.getAllByTestId("fork-pane");
      const rootPane = panes.find((p) => p.dataset.sessionId === "root")!;
      const f1Pane = panes.find((p) => p.dataset.sessionId === "f1")!;
      // root original label
      expect(within(rootPane).getByText("fork.original")).toBeTruthy();
      // meta span carries provider/model/effort
      const meta = rootPane.querySelector(".fork-pane-run-meta")!;
      expect(meta.textContent).toContain("Prov-provA");
      expect(meta.textContent).toContain("sonnet");
      expect(meta.textContent).toContain("high");
      // title attribute is the joined meta
      expect(meta.getAttribute("title")).toContain("Prov-provA");
      // unnamed fork → tab label "fork.fork 1", pane header "fork.fork"
      const tabs = within(screen.getByTestId("fork-tabs-strip")).getAllByRole("tab");
      expect(tabs[1].textContent).toBe("fork.fork 1");
      expect(f1Pane.querySelector(".fork-pane-label")!.textContent).toBe("fork.fork");
    });

    it("hides run-meta when no provider/model/effort present", () => {
      renderView();
      const rootPane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "root")!;
      expect(rootPane.querySelector(".fork-pane-run-meta")).toBeNull();
    });
  });

  describe("tab strip", () => {
    it("renders a tab per pane with active marker and switches focus on click", () => {
      const onSetFocus = vi.fn();
      renderView({ focusedSessionId: "root", onSetFocus });
      const strip = screen.getByTestId("fork-tabs-strip");
      const tabs = within(strip).getAllByRole("tab");
      expect(tabs).toHaveLength(2);
      expect(tabs[0].getAttribute("aria-selected")).toBe("true");
      expect(tabs[1].getAttribute("aria-selected")).toBe("false");
      fireEvent.click(tabs[1]);
      expect(onSetFocus).toHaveBeenCalledWith("f1");
    });

    it("marks a closed fork tab with the closed class", () => {
      const root = buildTree();
      const f1: Session = { id: "f1", name: "x", kind: "user", fork_point_seq: 2, fork_closed: true, messages: [] };
      root.forks = [f1];
      renderView({ tree: root });
      const tabs = within(screen.getByTestId("fork-tabs-strip")).getAllByRole("tab");
      expect(tabs[1].className).toContain("closed");
    });

    it("hides the tab strip when only one pane", () => {
      renderView({ tree: buildTree() });
      expect(screen.queryByTestId("fork-tabs-strip")).toBeNull();
    });
  });

  describe("focused-view toolbar", () => {
    // focusedViewEnabled defaults to true, so on desktop with a valid
    // focusedSessionId the focused toolbar is active from first render.
    it("toolbar is active by default and back returns to split", () => {
      renderView({ focusedSessionId: "root" });
      expect(screen.getByTestId("fork-focus-toolbar")).toBeTruthy();
      fireEvent.click(screen.getByTestId("fork-back-to-split"));
      expect(screen.queryByTestId("fork-focus-toolbar")).toBeNull();
    });

    it("expand re-enters focused view after leaving it", () => {
      renderView({ focusedSessionId: "root" });
      fireEvent.click(screen.getByTestId("fork-back-to-split"));
      expect(screen.queryByTestId("fork-focus-toolbar")).toBeNull();
      const rootPane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "root")!;
      fireEvent.click(within(rootPane).getByLabelText("fork.openFocusedAria"));
      expect(screen.getByTestId("fork-focus-toolbar")).toBeTruthy();
    });

    it("auto-disables focused view when the focused pane is stale", () => {
      renderView({ focusedSessionId: "ghost" });
      // focusedViewPane null → effect disables → no toolbar, normal grid
      expect(screen.queryByTestId("fork-focus-toolbar")).toBeNull();
      expect(screen.getByTestId("fork-grid").className).not.toContain("fork-split-grid-focused");
    });
  });

  describe("grid column style", () => {
    it("sizes only the focused pane by default (focused view active)", () => {
      renderView({ focusedSessionId: "root" });
      const cols = screen.getByTestId("fork-grid").getAttribute("style") || "";
      // focused (root) pane gets the flexible column, others collapse to 0fr
      expect(cols).toContain("minmax(220px, 1fr)");
      expect(cols).toContain("minmax(0, 0fr)");
    });

    it("uses repeat(n) columns after leaving focused view", () => {
      renderView({ focusedSessionId: "root" });
      fireEvent.click(screen.getByTestId("fork-back-to-split"));
      const style = screen.getByTestId("fork-grid").getAttribute("style") || "";
      expect(style).toContain("repeat(2");
    });

    it("uses a single 1fr column on mobile", () => {
      control.viewportMode = "mobile";
      renderView();
      expect(screen.getByTestId("fork-grid").getAttribute("style") || "").toContain("1fr");
    });
  });

  describe("mobile rendering", () => {
    it("renders only the focused pane on mobile", () => {
      control.viewportMode = "mobile";
      renderView({ focusedSessionId: "f1" });
      expect(screen.getAllByTestId("fork-pane").map((p) => p.dataset.sessionId)).toEqual(["f1"]);
    });
  });

  describe("close / reopen / delete", () => {
    it("close button fires onCloseFork (non-root, open)", () => {
      const onCloseFork = vi.fn();
      renderView({ onCloseFork });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      fireEvent.click(within(f1Pane).getByLabelText("Close this fork pane"));
      expect(onCloseFork).toHaveBeenCalledWith("f1");
    });

    it("closed non-root fork shows reopen + delete and hides focus actions", () => {
      const root = buildTree();
      const f1: Session = { id: "f1", name: "x", kind: "user", fork_point_seq: 2, fork_closed: true, messages: [] };
      root.forks = [f1];
      const { onReopenFork, onDeleteFork } = renderView({ tree: root });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      expect(within(f1Pane).queryByLabelText("fork.openFocusedAria")).toBeNull();
      fireEvent.click(within(f1Pane).getByText("fork.reopen"));
      expect(onReopenFork).toHaveBeenCalledWith("f1");
      fireEvent.click(within(f1Pane).getByText("fork.delete"));
      expect(onDeleteFork).toHaveBeenCalledWith("f1");
    });

    it("closed root shows the closed tag, no reopen/delete", () => {
      const root = buildTree();
      root.fork_closed = true;
      const { onReopenFork, onDeleteFork } = renderView({ tree: root });
      const rootPane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "root")!;
      expect(within(rootPane).getByText("fork.closed")).toBeTruthy();
      expect(within(rootPane).queryByText("fork.reopen")).toBeNull();
      expect(within(rootPane).queryByText("fork.delete")).toBeNull();
      expect(onReopenFork).not.toHaveBeenCalled();
      expect(onDeleteFork).not.toHaveBeenCalled();
    });

    it("delete button omitted when onDeleteFork not provided", () => {
      const root = buildTree();
      const f1: Session = { id: "f1", name: "x", kind: "user", fork_point_seq: 2, fork_closed: true, messages: [] };
      root.forks = [f1];
      renderView({ tree: root, onDeleteFork: undefined });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      expect(within(f1Pane).queryByText("fork.delete")).toBeNull();
    });
  });

  describe("focus radio", () => {
    it("focus radio reflects checked state and fires onSetFocus", () => {
      const onSetFocus = vi.fn();
      renderView({ focusedSessionId: "root", onSetFocus });
      const panes = screen.getAllByTestId("fork-pane");
      const rootRadio = panes.find((p) => p.dataset.sessionId === "root")!.querySelector(
        ".fork-pane-focus-radio",
      )!;
      const f1Radio = panes.find((p) => p.dataset.sessionId === "f1")!.querySelector(
        ".fork-pane-focus-radio",
      )!;
      expect(rootRadio.getAttribute("aria-checked")).toBe("true");
      expect(f1Radio.getAttribute("aria-checked")).toBe("false");
      fireEvent.click(f1Radio);
      expect(onSetFocus).toHaveBeenCalledWith("f1");
    });
  });

  describe("empty panes", () => {
    it("shows fork-specific empty message when a pane has no divergent messages", () => {
      const root = buildTree();
      const f1: Session = { id: "f1", name: "x", kind: "user", fork_point_seq: 2, messages: [] };
      root.forks = [f1];
      renderView({ tree: root });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      expect(within(f1Pane).getByText("fork.forkNoMessages")).toBeTruthy();
    });

    it("shows the root empty message when the root pane has no divergent turns", () => {
      // root holds only shared turns (all <= fork point) → root pane divergent
      // region is empty → "fork.noNewTurns".
      const shared = [msg("u1", "user", 1), msg("a1", "assistant", 2)];
      const root: Session = { id: "root", name: "Root", kind: "user", messages: [...shared], forks: [] };
      const f1: Session = {
        id: "f1",
        name: "x",
        kind: "user",
        fork_point_seq: 2,
        messages: [...shared, msg("u2", "user", 3)],
        forks: [],
      };
      root.forks = [f1];
      renderView({ tree: root });
      const rootPane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "root")!;
      expect(within(rootPane).getByText("fork.noNewTurns")).toBeTruthy();
    });
  });

  describe("load-older sentinel", () => {
    it("shared region shows a load-older button when root has older pages", () => {
      const root = buildTree();
      root.pagination = { oldest_loaded_seq: 1, has_older: true };
      renderView({ tree: root });
      const shared = screen.getByTestId("fork-shared");
      fireEvent.click(within(shared).getByText("chat.loadOlderMessages"));
      expect(control.triggers).toContainEqual(expect.stringContaining("messages:loadOlder:shared:root"));
    });

    it("shared load-older invokes onLoadOlderMessages for the root", () => {
      const onLoadOlderMessages = vi.fn().mockResolvedValue(true);
      const root = buildTree();
      root.pagination = { oldest_loaded_seq: 1, has_older: true };
      renderView({ tree: root, onLoadOlderMessages });
      fireEvent.click(
        within(screen.getByTestId("fork-shared")).getByText("chat.loadOlderMessages"),
      );
      expect(onLoadOlderMessages).toHaveBeenCalledWith("root", 1);
    });

    it("shared region shows a spinner while loading older", () => {
      const root = buildTree();
      root.pagination = { oldest_loaded_seq: 1, has_older: true };
      control.opInflight = true;
      renderView({ tree: root });
      expect(within(screen.getByTestId("fork-shared")).getByText("fork.loading")).toBeTruthy();
    });

    it("shared region shows an error + retry when load-older failed", () => {
      const root = buildTree();
      root.pagination = { oldest_loaded_seq: 1, has_older: true };
      control.scrollLoadError = "boom";
      renderView({ tree: root });
      const shared = screen.getByTestId("fork-shared");
      expect(within(shared).getByText("chat.loadOlderFailed")).toBeTruthy();
      fireEvent.click(within(shared).getByText("chat.sessionLoadRetry"));
      expect(control.triggers.length).toBeGreaterThan(0);
    });

    it("per-pane load-older button calls onLoadOlderMessages for that pane", () => {
      const onLoadOlderMessages = vi.fn().mockResolvedValue(true);
      const root = forkedTree();
      const f1 = root.forks![0];
      f1.pagination = { oldest_loaded_seq: 3, has_older: true };
      renderView({ tree: root, onLoadOlderMessages });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      fireEvent.click(within(f1Pane).getByText("chat.loadOlderMessages"));
      expect(onLoadOlderMessages).toHaveBeenCalledWith("f1", 3);
    });
  });

  describe("message grouping edges", () => {
    it("an orphan assistant turn (no preceding user) yields no group", () => {
      const shared = [msg("u1", "user", 1), msg("a1", "assistant", 2)];
      const root: Session = { id: "root", name: "Root", kind: "user", messages: [...shared], forks: [] };
      // f1 divergent region starts with an assistant message (no user pair)
      const f1: Session = {
        id: "f1",
        name: "x",
        kind: "user",
        fork_point_seq: 2,
        messages: [...shared, msg("a_orphan", "assistant", 5)],
        forks: [],
      };
      root.forks = [f1];
      renderView({ tree: root });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      // orphan assistant is skipped by the grouping loop → no turn group
      // renders (messages.length is 1 so the empty-state gate doesn't fire,
      // but MessageList emits nothing).
      expect(within(f1Pane).queryAllByTestId("tg")).toHaveLength(0);
    });
  });

  describe("run anchoring", () => {
    it("anchors runs by target_message_id and sinks unanchored runs to the last group", () => {
      const root = forkedTree();
      const f1 = root.forks![0];
      // f1 divergent pane msgs: u2,a2,u3 → groups (u2,a2), (u3 alone)
      const runs: RunInfo[] = [
        { run_id: "r1", target_message_id: "u2", kind: "agent" },
        { run_id: "r2", target_message_id: "a2", kind: "agent" },
        { run_id: "r3", target_message_id: null, kind: "agent" }, // unanchored
      ];
      renderView({ tree: root, runStateBySession: { f1: runs } });
      const f1Pane = screen
        .getAllByTestId("fork-pane")
        .find((p) => p.dataset.sessionId === "f1")!;
      const tgs = within(f1Pane).getAllByTestId("tg");
      // (u2,a2) group carries r1+r2; (u3, no response, last) sinks r3
      const byInit = Object.fromEntries(tgs.map((t) => [t.dataset.init, t]));
      expect(byInit.u2.dataset.runs).toBe("2");
      expect(byInit.u2.dataset.resp).toBe("a2");
      expect(byInit.u3.dataset.runs).toBe("1");
    });
  });

  describe("tab-strip touch swipe", () => {
    function strip() {
      return screen.getByTestId("fork-tabs-strip");
    }
    function touch(node: HTMLElement, phase: "start" | "move" | "end", x: number, y = 0) {
      const ctor = phase === "end" ? "changedTouches" : "touches";
      const evt =
        phase === "start"
          ? fireEvent.touchStart(node, { touches: [{ clientX: x, clientY: y, identifier: 0 }] })
          : phase === "move"
            ? fireEvent.touchMove(node, { touches: [{ clientX: x, clientY: y, identifier: 0 }] })
            : fireEvent.touchEnd(node, { changedTouches: [{ clientX: x, clientY: y, identifier: 0 }] });
      void evt;
    }

      it("left-swipe moves focus to next pane (LTR)", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "root", onSetFocus });
        touch(strip(), "start", 200);
        touch(strip(), "move", 150); // lock horizontal
        touch(strip(), "end", 100); // dx = -100
        expect(onSetFocus).toHaveBeenCalledWith("f1");
      });

      it("right-swipe moves focus to previous pane (LTR)", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "f1", onSetFocus });
        touch(strip(), "start", 100);
        touch(strip(), "move", 150);
        touch(strip(), "end", 200); // dx = +100 → prev = root
        expect(onSetFocus).toHaveBeenCalledWith("root");
      });

      it("RTL flips swipe direction", () => {
        document.documentElement.setAttribute("dir", "rtl");
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "f1", onSetFocus });
        touch(strip(), "start", 200);
        touch(strip(), "move", 150);
        touch(strip(), "end", 100); // dx=-100; RTL step flips → next=0 root
        expect(onSetFocus).toHaveBeenCalledWith("root");
      });

      it("ignores movement inside the deadzone", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "root", onSetFocus });
        touch(strip(), "start", 100);
        touch(strip(), "move", 104); // dx=4, dy=4 — under 10px deadzone, no lock
        touch(strip(), "end", 104);
        expect(onSetFocus).not.toHaveBeenCalled();
      });

      it("ignores small horizontal swipes under the 50px threshold", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "root", onSetFocus });
        touch(strip(), "start", 100);
        touch(strip(), "move", 130); // lock horizontal (dx=30)
        touch(strip(), "end", 75); // dx=-25 < 50
        expect(onSetFocus).not.toHaveBeenCalled();
      });

      it("ignores a vertical-dominant gesture", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "root", onSetFocus });
        touch(strip(), "start", 100, 100);
        touch(strip(), "move", 105, 200); // dy >> dx → vertical lock
        touch(strip(), "end", 105, 200);
        expect(onSetFocus).not.toHaveBeenCalled();
      });

      it("ignores multi-touch start", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "root", onSetFocus });
        fireEvent.touchStart(strip(), {
          touches: [
            { clientX: 200, clientY: 0, identifier: 0 },
            { clientX: 210, clientY: 0, identifier: 1 },
          ],
        });
        fireEvent.touchEnd(strip(), {
          changedTouches: [{ clientX: 100, clientY: 0, identifier: 0 }],
        });
        expect(onSetFocus).not.toHaveBeenCalled();
      });

      it("does not wrap past the last pane", () => {
        const onSetFocus = vi.fn();
        renderView({ focusedSessionId: "f1", onSetFocus });
        touch(strip(), "start", 200);
        touch(strip(), "move", 150);
        touch(strip(), "end", 100); // dx=-100 → next=2 out of bounds
        expect(onSetFocus).not.toHaveBeenCalled();
      });
  });

  describe("stale focus fallback", () => {
    it("falls back to pane index 0 when focused id is unknown", () => {
      renderView({ focusedSessionId: "ghost" });
      const tabs = within(screen.getByTestId("fork-tabs-strip")).getAllByRole("tab");
      expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    });
  });

  afterEach(() => {
    document.documentElement.removeAttribute("dir");
  });
});
