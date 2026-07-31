import { afterEach, describe, expect, it, vi } from "vitest";

import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { renderApp } from "./harness";
import { setOpenSessionTabIds, setSelectedProject } from "../src/utils/uiSelection";
import i18n from "../src/i18n";

/**
 * Full-stack reproduction: an assistant response must never collapse
 * without the user clicking the chevron.
 *
 * The whole app is mounted (App → useSession → sessionRegistry → Chat →
 * MessageBubble) against the mock backend, which serves `/api/sessions`
 * with the SAME contract the real endpoint has — paginated at 50 by
 * default. `backend/scripts/test_session_listing_page_scope.py` locks
 * that backend half: a no-params request returns one page, and a session
 * ranked below it is absent while still perfectly alive.
 *
 * The bug: `sessionRegistry` applied that page as a COMPLETE snapshot and
 * evicted every sid missing from it. The open session sits below page 1
 * here, so on the next `/api/sessions` refresh — which the registry fires
 * on every tab focus (`visibilitychange`/`focus`) — its entry vanished and
 * `useSessionMeta(sid).is_running` silently went false. `TurnGroup`'s
 * `defaultCollapsed = hasResponse && !isRunning` then folded the
 * already-rendered response shut, with no user interaction.
 *
 * The window is real: `isGroupRunning` only covers the streaming window,
 * so once the assistant message stops streaming while the session is still
 * working (post-stream tools, background work, delegation,
 * blocked-on-user), session-level `is_running` is the ONLY thing holding
 * the group open.
 */

const PAGE_SIZE = 50;
const TOTAL_SESSIONS = 60;
/** Ranks last under updated_at-desc ordering → never on page 1. */
const OPEN_SESSION_ID = `sess-${TOTAL_SESSIONS}`;

function seedSessions() {
  return Array.from({ length: TOTAL_SESSIONS }, (_, i) => {
    const index = i + 1;
    const isOpenOne = index === TOTAL_SESSIONS;
    return makeSession({
      id: `sess-${index}`,
      name: `Session ${index}`,
      cwd: "/tmp/project-a",
      // The open session is the OLDEST, so the default page (newest 50)
      // excludes it — exactly like the real backend's ranking.
      updated_at: isOpenOne
        ? "2026-01-01T00:00:00.000Z"
        : `2026-06-${String((i % 28) + 1).padStart(2, "0")}T00:00:00.000Z`,
      messages: isOpenOne
        ? [
            makeUserMsg({ id: "u1", content: "do the thing", seq: 0 }),
            makeAssistantMsg({
              id: "a1",
              content: "finished reply",
              seq: 1,
              isStreaming: false,
            }),
          ]
        : [],
    });
  });
}

function isExpanded(h: Awaited<ReturnType<typeof renderApp>>): boolean {
  return h.$(".collapse-arrow")?.textContent === "▼";
}

function responseVisible(h: Awaited<ReturnType<typeof renderApp>>): boolean {
  return (
    h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content') !==
    null
  );
}

/** Open the session, then mark it running at the SESSION level with the
 * assistant message already finished streaming — the exact state where
 * only `sessionRunning` holds the group open. */
async function openRunningSessionBelowPageOne() {
  window.history.pushState(null, "", `/s/${OPEN_SESSION_ID}`);
  localStorage.setItem(
    "better-agent-open-session-ids",
    JSON.stringify([OPEN_SESSION_ID]),
  );
  const h = await renderApp({
    seed: { sessions: seedSessions() },
    // Serve `/api/sessions` the way the real endpoint does — one page of
    // 50 — so the open session is genuinely off the page the registry
    // bootstraps from, instead of being handed the whole corpus.
    configureBackend: (backend) => backend.setSessionPageLimit(PAGE_SIZE),
  });
  // The turn mounts COLLAPSED — it is complete and nothing is running yet
  // (docs/chat-panel.md's render(turn)). Wait for the turn to exist...
  await h.waitFor(
    () => h.$('[data-testid="user-message"][data-message-id="u1"]') !== null,
    5000,
  );

  // ...then mark the SESSION running. The assistant message is already
  // done streaming, so `sessionRunning` alone now holds the group open.
  h.emit({
    type: "session_monitoring_changed",
    data: {
      session_id: OPEN_SESSION_ID,
      monitoring_state: "active",
      cwd: "/tmp/project-a",
      node_id: "primary",
    },
  });
  await h.waitFor(() => responseVisible(h), 3000);
  return h;
}

describe("chat panel — an assistant response never collapses on its own", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    setSelectedProject("", "primary");
    setOpenSessionTabIds([]);
    localStorage.removeItem("better-agent-open-session-ids");
    localStorage.removeItem("better-agent-open-session-joined-at");
    localStorage.removeItem("better-agent-selected-project");
    localStorage.removeItem("better-agent-selected-project-node");
    void i18n.changeLanguage("en");
    window.history.pushState(null, "", "/");
  });

  it("stays expanded when the tab regains focus and the session is below page 1", async () => {
    const h = await openRunningSessionBelowPageOne();

    expect(isExpanded(h)).toBe(true);
    expect(responseVisible(h)).toBe(true);
    expect(h.raw.container.textContent).toContain("finished reply");

    // Tab focus → sessionRegistry re-snapshots from `/api/sessions`. That
    // page cannot contain this session.
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await h.flush();
    await h.flush();

    const pageCalls = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/sessions",
    );
    expect(pageCalls.length).toBeGreaterThan(1);

    expect(isExpanded(h)).toBe(true);
    expect(responseVisible(h)).toBe(true);
    expect(h.raw.container.textContent).toContain("finished reply");
    h.unmount();
  }, 20000);

  it("stays expanded across repeated focus refreshes", async () => {
    const h = await openRunningSessionBelowPageOne();
    expect(isExpanded(h)).toBe(true);

    for (let i = 0; i < 3; i++) {
      document.dispatchEvent(new Event("visibilitychange"));
      await h.flush();
      window.dispatchEvent(new Event("focus"));
      await h.flush();
    }

    expect(isExpanded(h)).toBe(true);
    expect(responseVisible(h)).toBe(true);
    h.unmount();
  }, 20000);

  it("still collapses when the user clicks the chevron", async () => {
    const h = await openRunningSessionBelowPageOne();
    expect(isExpanded(h)).toBe(true);

    await h.click('.message-box-header-main[aria-expanded="true"]');

    expect(isExpanded(h)).toBe(false);
    expect(responseVisible(h)).toBe(false);

    // ...and a focus refresh does not silently re-open it either.
    document.dispatchEvent(new Event("visibilitychange"));
    await h.flush();
    expect(isExpanded(h)).toBe(false);
    h.unmount();
  }, 20000);
});
