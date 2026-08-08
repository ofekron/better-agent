// Coverage for `usePendingUserInteractions`'s v2 current-session hydration
// split (surface-migration Package A design ruling): the OPEN session's
// `input`-kind pending-interaction cards source from the v2
// `CompactSessionSnapshotWire.interactions` field (`GET /api/v2/surface/
// sessions/:id/snapshot`) instead of the legacy `GET /api/user-input/
// pending` list. Every scenario here seeds ONLY via `h.seedSurface` (the
// v2 wire shape) with NO matching legacy `userInputs` seed — a card
// rendering at all proves the v2 read path is genuinely wired (before
// this migration the hook never called `fetchSnapshot`, so these cases
// would render nothing).
//
// Seeded via `configureBackend` (runs before the app's own first mount/
// auto-open-remembered-session fetch), not a post-render `h.seedSurface`
// call — `usePendingUserInteractions`'s v2 hydration effect only re-fires
// on a `currentSessionId` CHANGE (mirrors `useSurfaceStore`'s own cursor-
// resubscribe trigger), so seeding after the app has already auto-opened
// the lone seeded session would never be observed by a same-id
// `selectSession` (confirmed empirically while writing this file: the app
// auto-opens a session with no other candidates during `renderApp`'s own
// initial flush, racing ahead of a later `h.seedSurface` call for that
// same id).

import { describe, expect, it } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { UserInteractionWire } from "../src/adapter/wire";

function v2Question(requestId: string, question: string): UserInteractionWire {
  return {
    interaction_ref: `user_input:${requestId}`,
    kind: "input",
    state: "pending",
    response: null,
    request: {
      schema: { kind: "questions", questions: [{ id: "q1", header: "H", question, options: [] }] },
    },
  };
}

function v2Approval(requestId: string, prompt: string): UserInteractionWire {
  return {
    interaction_ref: `user_input:${requestId}`,
    kind: "input",
    state: "pending",
    response: null,
    request: { schema: { kind: "approval" }, prompt },
  };
}

describe("usePendingUserInteractions — v2 current-session hydration", () => {
  it("renders a card for the OPEN session sourced purely from the v2 snapshot's `interactions` field", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface(session.id, { interactions: [v2Question("v2-req-1", "Which environment?")] });
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const card = h.$('[data-testid="user-input-card"]');
    expect(card?.textContent).toContain("Which environment?");
    h.unmount();
  });

  it("maps the `approval` sub-kind (schema.kind==='approval') onto the approval card, same as the legacy path", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface(session.id, { interactions: [v2Approval("v2-appr-1", "Deploy the release?")] });
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const card = h.$('[data-testid="user-approval-card"]');
    expect(card?.textContent).toContain("Deploy the release?");
    h.unmount();
  });

  it("a resolved card (via legacy REST fallback, no live socket in this harness) is removed from the v2-sourced list", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface(session.id, { interactions: [v2Approval("v2-appr-2", "Ship it?")] });
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.$('[data-testid="user-approval-card"]')).not.toBeNull();
    await h.click('[data-testid="user-approval-card"] button[data-action="approve"]');
    await h.flush();

    expect(
      h.restCalls.find(
        (c) => c.method === "POST" && c.path === "/api/user-input/v2-appr-2/resolve",
      ),
    ).toBeDefined();
    expect(h.$('[data-testid="user-approval-card"]')).toBeNull();
    h.unmount();
  });

  it("switching sessions scopes v2 hydration to the newly-open session (no cross-session leak)", async () => {
    const sessionA = makeSession({ id: "sess-a" });
    const sessionB = makeSession({ id: "sess-b" });
    const h = await renderApp({
      seed: { sessions: [sessionA, sessionB] },
      configureBackend: (backend) => {
        backend.seedSurface(sessionA.id, { interactions: [v2Question("v2-a", "Question for A?")] });
        backend.seedSurface(sessionB.id, { interactions: [v2Question("v2-b", "Question for B?")] });
      },
    });

    await h.selectSession(sessionA.id);
    await h.flush();
    expect(h.$('[data-testid="user-input-card"]')?.textContent).toContain("Question for A?");

    await h.selectSession(sessionB.id);
    await h.flush();
    expect(h.$('[data-testid="user-input-card"]')?.textContent).toContain("Question for B?");
    expect(h.$('[data-testid="user-input-card"]')?.textContent).not.toContain("Question for A?");
    h.unmount();
  });
});
