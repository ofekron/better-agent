import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { eventBus } from "../src/lib/eventBus";
import { sessionRegistry } from "../src/lib/sessionRegistry";
import { SessionStatusBadge } from "../src/components/SessionStatusBadge";
import { ProjectStatusBadge } from "../src/components/ProjectStatusBadge";

/**
 * Rendering tests for the two status-indicator surfaces.
 *
 * The registry reducer is covered by sessionRegistry.test.ts; this file
 * covers what the user actually SEES — that every status dimension
 * reaches the screen, that dimensions never hide one another, and that
 * a pushed backend change repaints without a refetch.
 *
 * Both badges are shared components: the sidebar row, the open tab, and
 * the project tab all render these exact elements, so pinning them here
 * pins every surface that embeds them.
 *
 * Two known product gaps are asserted as the current truth rather than
 * papered over:
 *   • "Awaiting background work" has no visual distinct from "Running".
 *   • "Is done" has no dedicated badge — it surfaces only as the generic
 *     marker dot the user-attention extension paints.
 */

const NEEDS_DECISION = "NEEDS_USER_DECISION";
const ALL_TASKS_DONE = "ALL_TASKS__DONE";

const PROJECT = "/p";

function stubSessions(sessions: unknown[]) {
  (globalThis as unknown as { fetch: typeof fetch }).fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => ({ sessions }) }) as unknown as typeof fetch;
}

async function resetRegistry(sessions: unknown[] = []) {
  // Detach the previous test's bus subscriptions before re-binding —
  // otherwise every reset stacks another subscriber on the singleton and
  // one publish fans out to all of them.
  sessionRegistry.unbind();
  (sessionRegistry as unknown as { __resetForTests: () => void }).__resetForTests();
  sessionRegistry.bind();
  stubSessions(sessions);
  await sessionRegistry.bootstrap();
}

/** Drain the badge's running-pulse debounce (0ms on rising edge, 100ms
 * on falling) so assertions see the settled indicator. */
async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 120));
  });
}

function indicators(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll("[data-testid]"))
    .map((el) => el.getAttribute("data-testid") as string)
    .sort();
}

afterEach(cleanup);

// ── Per-session badge: the full dimension matrix ────────────────────────

type Combo = {
  running: "active" | "waiting_on_background" | "stopped";
  waiting: boolean;
  unread: boolean;
  errored: boolean;
  done: boolean;
};

const COMBOS: Combo[] = [];
for (const running of ["active", "waiting_on_background", "stopped"] as const) {
  for (const waiting of [false, true]) {
    for (const unread of [false, true]) {
      for (const errored of [false, true]) {
        for (const done of [false, true]) {
          COMBOS.push({ running, waiting, unread, errored, done });
        }
      }
    }
  }
}

/** What the badge is expected to paint for one combination. */
function expectedIndicators(c: Combo): string[] {
  const out: string[] = [];
  if (c.errored) out.push("session-error-dot");
  if (c.waiting) out.push("session-user-input-dot");
  // The done marker is a marker like any other, so it paints the generic
  // marker dot — there is no dedicated "done" badge yet.
  if (c.done) out.push("session-marker-dot");
  // Background work is painted with the same pulse as foreground work.
  if (c.running !== "stopped") out.push("session-running-pulse");
  if (c.unread) out.push("session-unread-count");
  return out.sort();
}

async function seedCombo(sid: string, c: Combo) {
  await resetRegistry([{ id: sid, cwd: PROJECT, node_id: "primary" }]);
  eventBus.publish("session_monitoring_changed", {
    session_id: sid,
    monitoring_state: c.running,
    cwd: PROJECT,
    node_id: "primary",
  });
  if (c.waiting) {
    eventBus.publish("session_user_input_changed", {
      session_id: sid,
      pending_user_input_count: 1,
    });
  }
  if (c.unread) {
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 3,
      cwd: PROJECT,
      node_id: "primary",
    });
  }
  if (c.errored) {
    eventBus.publish("session_error_changed", { session_id: sid, has_error: true });
  }
  if (c.done) {
    eventBus.publish("session_marker_changed", {
      session_id: sid,
      extension_id: "user-attention",
      marker: { color: "#08f", tooltip: "done", tag: ALL_TASKS_DONE },
    });
  }
}

describe("SessionStatusBadge — every status dimension reaches the screen", () => {
  // A fresh mount seeds the running-pulse debounce with the settled
  // value, so no timer drain is needed between combinations.
  it(`renders the expected indicator set for all ${COMBOS.length} dimension combinations`, async () => {
    const wrong: string[] = [];
    for (const c of COMBOS) {
      const sid = "matrix-sid";
      await seedCombo(sid, c);
      const { container, unmount } = render(<SessionStatusBadge sid={sid} />);
      const got = indicators(container);
      const want = expectedIndicators(c);
      if (JSON.stringify(got) !== JSON.stringify(want)) {
        wrong.push(
          `${c.running}/w=${c.waiting}/u=${c.unread}/e=${c.errored}/d=${c.done}` +
            ` got=[${got}] want=[${want}]`,
        );
      }
      unmount();
    }
    expect(wrong).toEqual([]);
  });

  it("an idle session with nothing to report renders no indicator at all", async () => {
    await seedCombo("quiet", {
      running: "stopped",
      waiting: false,
      unread: false,
      errored: false,
      done: false,
    });
    const { container } = render(<SessionStatusBadge sid="quiet" />);
    await settle();
    expect(indicators(container)).toEqual([]);
  });

  it("dimensions never mask each other — errored + waiting + running + unread all show", async () => {
    await seedCombo("loud", {
      running: "active",
      waiting: true,
      unread: true,
      errored: true,
      done: false,
    });
    const { container } = render(<SessionStatusBadge sid="loud" />);
    await settle();
    expect(indicators(container)).toEqual([
      "session-error-dot",
      "session-running-pulse",
      "session-unread-count",
      "session-user-input-dot",
    ]);
  });

  it("an approval blocking the turn paints the approval pulse, not the running pulse", async () => {
    await resetRegistry([{ id: "appr", cwd: PROJECT, node_id: "primary" }]);
    eventBus.publish("session_monitoring_changed", {
      session_id: "appr",
      monitoring_state: "blocked_on_user",
      cwd: PROJECT,
      node_id: "primary",
    });
    const { container } = render(<SessionStatusBadge sid="appr" />);
    await settle();
    expect(indicators(container)).toEqual(["session-approval-pulse"]);
  });

  it("repaints from a pushed backend change with no refetch", async () => {
    await resetRegistry([{ id: "push", cwd: PROJECT, node_id: "primary" }]);
    const { container } = render(<SessionStatusBadge sid="push" />);
    await settle();
    expect(indicators(container)).toEqual([]);

    await act(async () => {
      eventBus.publish("session_error_changed", { session_id: "push", has_error: true });
    });
    await settle();
    expect(indicators(container)).toContain("session-error-dot");

    // A framing event cannot clear backend-owned error state.
    await act(async () => {
      eventBus.publish("turn_start", { app_session_id: "push" });
    });
    await settle();
    expect(indicators(container)).toContain("session-error-dot");

    await act(async () => {
      eventBus.publish("session_error_changed", { session_id: "push", has_error: false });
      eventBus.publish("session_monitoring_changed", {
        session_id: "push",
        monitoring_state: "active",
        cwd: PROJECT,
        node_id: "primary",
      });
    });
    await settle();
    expect(indicators(container)).not.toContain("session-error-dot");
    expect(indicators(container)).toContain("session-running-pulse");
  });

  it("shows the unread count when asked, and caps it at 99+", async () => {
    await resetRegistry([{ id: "many", cwd: PROJECT, node_id: "primary" }]);
    eventBus.publish("session_unread_changed", {
      session_id: "many",
      unread_count: 150,
      cwd: PROJECT,
      node_id: "primary",
    });
    const { container } = render(<SessionStatusBadge sid="many" showUnreadCount />);
    await settle();
    expect(
      container.querySelector('[data-testid="session-unread-count"]')?.textContent,
    ).toBe("99+");
  });
});

// ── Project badge: one counter per dimension ────────────────────────────

describe("ProjectStatusBadge — one counter per dimension", () => {
  beforeEach(async () => {
    await resetRegistry([
      { id: "a", cwd: PROJECT, node_id: "primary" },
      { id: "b", cwd: PROJECT, node_id: "primary" },
      { id: "c", cwd: PROJECT, node_id: "primary" },
    ]);
  });

  function counts(container: HTMLElement): Record<string, string> {
    const out: Record<string, string> = {};
    for (const el of Array.from(container.querySelectorAll("[data-testid]"))) {
      out[el.getAttribute("data-testid") as string] = el.textContent ?? "";
    }
    return out;
  }

  it("renders nothing when the project has no signal", () => {
    const { container } = render(<ProjectStatusBadge path={PROJECT} />);
    expect(indicators(container)).toEqual([]);
  });

  it("renders each dimension's counter independently", async () => {
    await act(async () => {
      eventBus.publish("session_monitoring_changed", {
        session_id: "a",
        monitoring_state: "active",
        cwd: PROJECT,
        node_id: "primary",
      });
      eventBus.publish("session_unread_changed", {
        session_id: "b",
        unread_count: 2,
        cwd: PROJECT,
        node_id: "primary",
      });
      eventBus.publish("session_user_input_changed", {
        session_id: "b",
        pending_user_input_count: 1,
      });
      eventBus.publish("session_error_changed", { session_id: "c", has_error: true });
    });
    const { container } = render(<ProjectStatusBadge path={PROJECT} />);
    expect(counts(container)).toEqual({
      "project-running-count": "1",
      "project-unread-count": "1",
      "project-waiting-count": "1",
      "project-errored-count": "1",
    });
  });

  it("a project whose only signal is an error still shows a counter", async () => {
    await act(async () => {
      eventBus.publish("session_error_changed", { session_id: "a", has_error: true });
    });
    const { container } = render(<ProjectStatusBadge path={PROJECT} />);
    expect(counts(container)).toEqual({ "project-errored-count": "1" });
  });

  it("updates live when a pushed change moves a counter", async () => {
    const { container } = render(<ProjectStatusBadge path={PROJECT} />);
    expect(indicators(container)).toEqual([]);
    await act(async () => {
      eventBus.publish("session_user_input_changed", {
        session_id: "a",
        pending_user_input_count: 1,
      });
    });
    expect(counts(container)).toEqual({ "project-waiting-count": "1" });
    await act(async () => {
      eventBus.publish("session_user_input_changed", {
        session_id: "a",
        pending_user_input_count: 0,
      });
    });
    expect(indicators(container)).toEqual([]);
  });

  it("counters are scoped to their own project", async () => {
    await resetRegistry([
      { id: "mine", cwd: PROJECT, node_id: "primary" },
      { id: "theirs", cwd: "/other", node_id: "primary" },
    ]);
    await act(async () => {
      eventBus.publish("session_error_changed", { session_id: "theirs", has_error: true });
    });
    const { container } = render(<ProjectStatusBadge path={PROJECT} />);
    expect(indicators(container)).toEqual([]);
  });
});
