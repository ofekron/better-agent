// Migrated from the legacy `run_state` badge suite (backend-owned run
// mirroring under MessageBubble.tsx's RunBadge) to the native surface
// path. Native investigation finding (see phase2-inventory.md's
// runs.test.ts entry, now resolved): the wire already carries a genuine
// per-turn live-activity signal — `turn_lifecycle` frames feed
// `TurnEntry.phase` (state.ts), and `isLivePhase(phase)` was already
// consumed to gate live body-fetching but never rendered anywhere. This
// suite proves the new `RunningIndicator` (surface/leaf/RunningIndicator.tsx),
// wired into TurnView.tsx, renders/clears off that existing signal.
//
// Structural difference from legacy (not a regression — see each deleted
// case below for the specific reason): the native contract-node model
// carries ONE phase enum per turn, not a list of kind-labeled run objects
// (`run_state.runs[]` with `kind: manager|worker|native`,
// `target_message_id` anchoring, `delegation_id`). Every legacy case
// whose *distinguishing* content was the run-object list shape rather
// than "is something live right now" is deleted with its own reason,
// not ported as a vacuous assertion.
//
// Seeded via `configureBackend` (not a post-`renderApp()` `h.seedSurface()`
// call) per tests/surfaceHarness.test.tsx's header comment: with a single
// session, `autoSelectSession` can win the race against a `seedSurface()`
// called after `renderApp()` resolves, silently hydrating from the
// always-empty default envelope.

import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { turnNode, promptNode, compactTurn, turnLifecycleFrame } from "./surface/fixtures";

describe("native run-state indicator (turn_lifecycle-driven)", () => {
  it("shows the live running indicator while the turn's phase is live, clears on completion", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-run", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-run", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "go"), [])],
        });
      },
    });

    await h.selectSession("s-run");
    await h.waitFor(() => h.$('[data-testid="surface-turn"][data-turn-id="t1"]') !== null);

    // No lifecycle frame observed yet — not live.
    expect(h.$('[data-testid="surface-run-badge"]')).toBeNull();

    h.emitSurface("s-run", turnLifecycleFrame("t1", "running"));
    await h.waitFor(() => h.$('[data-testid="surface-run-badge"]') !== null);

    const turnEl = h.$('[data-testid="surface-turn"][data-turn-id="t1"]');
    expect(turnEl?.querySelector('[data-testid="surface-run-badge"]')).not.toBeNull();
    expect(turnEl?.querySelector('[data-testid="surface-run-badge"]')?.textContent).toContain("running");

    h.emitSurface("s-run", turnLifecycleFrame("t1", "completed"));
    await h.waitFor(() => h.$('[data-testid="surface-run-badge"]') === null);

    expect(h.$('[data-testid="surface-run-badge"]')).toBeNull();
    h.unmount();
  });

  it("is scoped to the actively running turn, not other turns rendered in the same view", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-scope", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-scope", {
          turns: [
            compactTurn(turnNode("t1"), promptNode("t1", "first"), []),
            compactTurn(turnNode("t2"), promptNode("t2", "second"), []),
          ],
        });
      },
    });

    await h.selectSession("s-scope");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);

    h.emitSurface("s-scope", turnLifecycleFrame("t2", "starting"));
    await h.waitFor(() => h.$('[data-testid="surface-run-badge"]') !== null);

    const turns = h.$$('[data-testid="surface-turn"]');
    const turn1 = turns.find((t) => t.getAttribute("data-turn-id") === "t1");
    const turn2 = turns.find((t) => t.getAttribute("data-turn-id") === "t2");
    expect(turn1?.querySelector('[data-testid="surface-run-badge"]')).toBeNull();
    expect(turn2?.querySelector('[data-testid="surface-run-badge"]')).not.toBeNull();
    h.unmount();
  });

  it("every LIVE_PHASES value shows the indicator; every terminal phase hides it", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-phases", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-phases", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "go"), [])],
        });
      },
    });

    await h.selectSession("s-phases");
    await h.waitFor(() => h.$('[data-testid="surface-turn"][data-turn-id="t1"]') !== null);

    const livePhases = ["queued", "starting", "running", "awaiting_interaction", "reconnecting", "stopping"] as const;
    const terminalPhases = ["completed", "stopped", "failed"] as const;

    for (const phase of livePhases) {
      h.emitSurface("s-phases", turnLifecycleFrame("t1", phase));
      await h.waitFor(() => h.$('[data-testid="surface-run-badge"]') !== null);
    }

    for (const phase of terminalPhases) {
      h.emitSurface("s-phases", turnLifecycleFrame("t1", phase));
      await h.waitFor(() => h.$('[data-testid="surface-run-badge"]') === null);
    }

    h.unmount();
  });

  // --- Deleted, not ported (each with its structural reason) ----------
  //
  // "an unanchored manager run renders 'manager running' under the user
  // bubble" / "...with a missing target renders under the user bubble" /
  // "an anchored run renders inside its target assistant bubble": legacy's
  // anchored-vs-unanchored distinction existed because a run_state entry
  // was a separate object that had to be POSITIONED relative to a
  // `target_message_id`. Natively there is no separate run object to
  // anchor — the live indicator is data ON the turn itself (TurnEntry.phase),
  // so there is only one placement, always inside the owning turn. Replaced
  // by "is scoped to the actively running turn" above, which is the native
  // equivalent of the anchoring guarantee (right turn, not some other one).
  //
  // "a worker run renders with kind='worker' and the worker description":
  // no native equivalent constructible. `worker_turn`/`sub_session_turn`
  // container nodes are declared in the contract (backend/surface_contract/
  // nodes.py) but are never constructed by any of the three node-building
  // adapters today (confirmed gap, documented in surface/liveness.ts's
  // header comment) — there is no backend-emitted node this case could
  // seed. This is a separate, already-tracked backend gap, not something
  // this suite's scope covers.
  //
  // "multiple runs (manager + worker) render simultaneously": legacy's
  // run_state.runs[] was a list of independently kind-labeled entries.
  // Natively there is one phase enum per turn — there is no "two
  // differently-kinded runs on the same turn" state to construct. (Two
  // DIFFERENT turns simultaneously live is exercised structurally by the
  // "scoped to the actively running turn" case above, which needs no
  // kind label to prove per-turn independence.)
  //
  // "run_state for a non-current session does NOT render badges in the
  // current view": native session isolation is structural, not a
  // client-side filter step to regression-test. `SurfaceStore.applyFrame`
  // (surface/state.ts) never checks `frame.surface_id` against its own
  // session id — it trusts the transport boundary, because in production
  // exactly one `/ws/v2/surface` socket is open per rendered session and
  // a frame for another session physically cannot arrive on it. The test
  // harness's mock models exactly one active surface socket at a time
  // (tests/harness/mockWebSocket.ts's `emitTo` matches "whichever surface
  // socket is currently open", not a specific session), so routing a frame
  // at an unmounted second session's id would actually be misdelivered
  // onto the *current* session's real socket — testing a mock artifact,
  // not the real isolation property, which the native protocol guarantees
  // by construction instead of by client-side filtering.
  //
  // "native run kind renders only 'Running...' under the user bubble":
  // legacy hardcoded the untranslated literal "Running..." for the
  // `kind: "native"` case specifically (RunBadge.tsx's `label` ternary).
  // The native indicator has no kind concept and always uses the existing
  // `runBadge.running` i18n key (all locales) — covered by the first case
  // above (asserts the badge's translated text), which is strictly BETTER
  // (i18n-conformant) rather than reproducing the legacy hardcoding.
  //
  // "chat running follows session monitoring when run_state detail is
  // absent" / "chat run badge disappears when monitoring stops before
  // run_state clears": both exercise legacy's separate
  // `session_monitoring_changed` WS event as a second, independent
  // "is this session running" signal that could race or diverge from
  // `run_state`. No `monitoring` concept exists anywhere in the native
  // wire contract (adapter/wire.ts) — `turn_lifecycle`'s `phase` is the
  // SOLE liveness signal, delivered atomically in one frame, so there is
  // no second signal source for it to be out of sync with, and no
  // "detail absent but still running" state to construct.
});
