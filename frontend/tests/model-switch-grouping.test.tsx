import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { promptNode, turnNode, compactTurn, modelChangeNode } from "./surface/fixtures";

/**
 * A model switch is recorded against the previous turn's assistant message
 * (the only message that exists at switch time) but takes effect on the NEXT
 * turn. In the native contract-node model this is represented directly:
 * `model_change`/`harness_change` are data ON the turn they affect
 * (`CompactTurnWire.runtime_change`), rendered by TurnView.tsx as a boundary
 * BEFORE that turn's own content — there is no separate "attach it to the
 * previous turn and reposition at render time" step to test; the shape
 * itself makes "always precedes the turn it affects" the only representable
 * placement.
 *
 * Seeded via `configureBackend` (not a post-`renderApp()` `h.seedSurface()`
 * call): with a single session, `autoSelectSession` fires and the
 * newly-mounted `ChatSurfaceView`'s initial `fetchSnapshot()` can win the
 * race against a `seedSurface()` called after `renderApp()` resolves,
 * silently hydrating from the always-empty default envelope instead of the
 * intended seed. `configureBackend` runs before the app ever mounts, so it
 * is the only ordering that reliably backs the FIRST render of a
 * REST-seeded (non-live) turn — see tests/surfaceHarness.test.tsx's header
 * comment, which documents the post-render `seedSurface()` shape but that
 * pattern only reliably backs the LIVE-frame path.
 */
describe("model-switch event grouping", () => {
  it("renders the switch banner preceding the next user prompt, not under the previous group", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-switch", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-switch", {
          turns: [
            compactTurn(turnNode("t1"), promptNode("t1", "first prompt"), []),
            compactTurn(
              turnNode("t2"),
              promptNode("t2", "second prompt"),
              [],
              modelChangeNode("t2", "claude / sonnet", "codex / gpt-5-codex"),
            ),
          ],
        });
      },
    });

    await h.selectSession("sess-switch");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);

    const turns = h.$$('[data-testid="surface-turn"]');
    const turn1 = turns.find((t) => t.getAttribute("data-turn-id") === "t1");
    const turn2 = turns.find((t) => t.getAttribute("data-turn-id") === "t2");
    expect(turn1).toBeTruthy();
    expect(turn2).toBeTruthy();

    // Banner belongs to the SECOND turn (heads the turn it affects)…
    const banner = turn2!.querySelector('[data-testid="surface-model-change"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("claude / sonnet → codex / gpt-5-codex");
    // …and must NOT appear under the first (unaffected) turn.
    expect(turn1!.querySelector('[data-testid="surface-model-change"]')).toBeNull();

    h.unmount();
  });

  // The legacy "trailing switch banner as a preface when no next prompt
  // exists yet" case is deleted, not ported: in the legacy client-side
  // model a model_switched event lived on the PREVIOUS assistant message
  // and had to be repositioned at render time when no next turn existed
  // yet. In the native contract-node model, model_change/harness_change is
  // data attached directly to the turn it affects (CompactTurnWire.
  // runtime_change) — it cannot exist without that turn, so there is no
  // "floating trailing banner with no owning turn yet" state to construct
  // or observe. The backend only emits the runtime_change once the turn it
  // describes exists, at which point it renders via the (ported) case
  // above.
});
