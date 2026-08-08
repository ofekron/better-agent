import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { NodeUpsertFrame, TransportAckFrame, TypedPromptPayloadWire } from "../src/adapter/wire";

/**
 * Native migration of the legacy pendingMessages/client_id suite (Phase I
 * stage 2c) — the native send path's own client-generated `intent_id`
 * (SurfaceStore.sendPrompt, state.ts) now exists, closing this file's
 * premise. Legacy's `upsertPendingUnlessAcked` (src/utils/pendingMessages.ts)
 * kept ONE shared, session-keyed pending list mutated by ack/error EVENTS;
 * native instead scaffolds a provisional turn directly INSIDE the sending
 * session's own disposable SurfaceStore instance (one instance per
 * sessionId, see src/surface/useSurfaceStore.ts) and reconciles it via the
 * confirmed node's own `intent_id` payload field — a structurally
 * different mechanism, so this file is a behavior-preserving rewrite, not
 * a mechanical find/replace. See the bottom of this file for the legacy
 * cases with no native equivalent (deleted, reasons documented inline).
 */

function confirmedTypedPromptFrame(params: {
  sessionId: string;
  turnId: string;
  nodeId: string;
  text: string;
  intentId: string;
  renderRev: number;
}): NodeUpsertFrame {
  const payload: TypedPromptPayloadWire = {
    text: params.text,
    attachments: [],
    send_mode: "queue",
    origin: "user",
    source_session_ref: null,
    sent_text: null,
    intent_id: params.intentId,
  };
  return {
    type: "node_upsert",
    cv: 1,
    surface_id: params.sessionId,
    snapshot: { incarnation: "mock", render_rev: params.renderRev, hist_rev: 0 },
    node: {
      cv: 1,
      node_id: params.nodeId,
      parent_id: null,
      turn_id: params.turnId,
      surface_id: params.sessionId,
      kind: "typed_prompt",
      ts: 1000 + params.renderRev,
      seq: 0,
      status: null,
      payload,
      run_ref: null,
      sidecar_ref: null,
      target_ref: null,
      child_manifest: null,
    },
  };
}

function outboundIntentId(outbound: readonly { intent?: { kind?: string; intent_id?: string } }[]): string {
  const sent = outbound.find((f) => f.intent?.kind === "send_prompt");
  if (!sent?.intent?.intent_id) throw new Error("no send_prompt intent found in outbound frames");
  return sent.intent.intent_id;
}

describe("native send: client-generated intent_id + provisional-turn reconciliation", () => {
  it("send inserts a provisional (sending) typed_prompt; the confirmed node (matching intent_id) reconciles it", async () => {
    localStorage.setItem("ba.surface_native", "1");
    // Each case in this file uses its OWN session id — real sessions never
    // share one, and `ProvisionalSendRegistry` (state.ts) is now durable
    // per session id ACROSS a store's own lifetime (that's the whole point
    // of surviving a session switch) — reusing "sess-1" across cases in
    // this same module (no `vi.resetModules()` here, unlike
    // tests/surface/nativeSend.test.tsx) would leak an unreconciled
    // provisional entry from one case's send into the next case's fresh
    // `renderApp()` for the "same" session, exactly like a real leftover
    // pending send would on a real reused session id.
    const session = makeSession({ id: "sess-client-id-1" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    await h.typeAndSend("first");

    const intentId = outboundIntentId(h.outbound as never);
    expect(h.$$('[data-testid="surface-prompt-status"]').length).toBe(1);
    expect(h.$('[data-testid="surface-prompt-status"]')?.className).toContain("status-sending");

    h.emitSurfaceRaw({ type: "intent_accepted", intent_id: intentId } satisfies TransportAckFrame);
    h.emitSurface(
      session.id,
      confirmedTypedPromptFrame({
        sessionId: session.id,
        turnId: "turn-first",
        nodeId: "u-first",
        text: "first",
        intentId,
        renderRev: 1,
      }),
    );
    await h.flush();

    expect(h.$$('[data-testid="surface-typed-prompt"]').length).toBe(1);
    expect(h.$('[data-testid="surface-prompt-status"]')).toBeNull();
    expect(h.$('[data-testid="surface-typed-prompt"]')?.textContent).toContain("first");
    h.unmount();
  });

  it("a late intent_rejected ack after the send already reconciled does not resurrect an error state", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-client-id-2" }); // see test 1's comment on per-case ids
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    await h.typeAndSend("accepted");
    const intentId = outboundIntentId(h.outbound as never);

    h.emitSurface(
      session.id,
      confirmedTypedPromptFrame({
        sessionId: session.id,
        turnId: "turn-accepted",
        nodeId: "u-accepted",
        text: "accepted",
        intentId,
        renderRev: 1,
      }),
    );
    await h.flush();
    expect(h.$('[data-testid="surface-prompt-status"]')).toBeNull();

    // Late rejection for the SAME intent_id, after reconciliation already
    // dropped it from SurfaceStore's pendingByIntentId map — must be a
    // no-op (failProvisionalSend's own early-return), not a resurrected
    // error bubble on the now-confirmed turn.
    h.emitSurfaceRaw({
      type: "intent_rejected",
      intent_id: intentId,
      code: "rejected",
      message: "late transport error",
    } satisfies TransportAckFrame);
    await h.flush();

    expect(h.$('[data-testid="surface-prompt-status"]')).toBeNull();
    expect(h.$$('[data-testid="surface-typed-prompt"]').length).toBe(1);
    expect(h.$('[data-testid="surface-typed-prompt"]')?.textContent).toContain("accepted");
    h.unmount();
  });

  it("each send carries a unique, non-empty intent_id", async () => {
    localStorage.setItem("ba.surface_native", "1");
    // Never reconciled/rejected — its 3 provisional sends stay "sending"
    // forever, so this MUST be its own session id (see test 1's comment).
    const session = makeSession({ id: "sess-client-id-3" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    await h.typeAndSend("a");
    await h.typeAndSend("b");
    await h.typeAndSend("c");

    const ids = (h.outbound as { intent?: { kind?: string; intent_id?: string } }[])
      .filter((f) => f.intent?.kind === "send_prompt")
      .map((f) => f.intent!.intent_id);
    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    expect(ids.every((id) => typeof id === "string" && id!.length > 0)).toBe(true);
    h.unmount();
  });

  it("a rejected send shows an inline error with Retry, and Retry re-submits under a fresh intent_id", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-client-id-4" }); // see test 1's comment on per-case ids
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    await h.typeAndSend("will fail");
    const firstIntentId = outboundIntentId(h.outbound as never);

    h.emitSurfaceRaw({
      type: "intent_rejected",
      intent_id: firstIntentId,
      code: "rejected",
      message: "no session selected",
    } satisfies TransportAckFrame);
    await h.waitFor(() => h.$('[data-testid="surface-prompt-status"]')?.className.includes("status-error") ?? false);

    await h.clickByText("Retry");
    await h.flush();

    const sendIntents = (h.outbound as { intent?: { kind?: string; intent_id?: string } }[]).filter(
      (f) => f.intent?.kind === "send_prompt",
    );
    expect(sendIntents).toHaveLength(2);
    expect(sendIntents[1].intent!.intent_id).not.toBe(firstIntentId);
    // Retry replaces the failed provisional turn, not appends alongside it.
    expect(h.$$('[data-testid="surface-typed-prompt"]').length).toBe(1);
    expect(h.$('[data-testid="surface-prompt-status"]')?.className).toContain("status-sending");
    h.unmount();
  });
});

/**
 * Legacy cases with NO native equivalent, deleted rather than ported:
 *
 * - "does not append pending when exact client_id was already acked" /
 *   "...when legacy no-client ack already cleared the session" /
 *   "replaces an existing optimistic failure with one fresh retry" /
 *   "removes the old failure without appending when the retry ack wins
 *   the race" (+ "...after a legacy retry ack") — all four are DIRECT
 *   unit tests of `upsertPendingUnlessAcked` (src/utils/pendingMessages.ts),
 *   a pure function over a shared, session-keyed pending LIST that only
 *   the legacy `/ws/chat` path maintains. Native has no equivalent
 *   function — reconciliation is internal SurfaceStore state keyed by
 *   `pendingByIntentId`, not a list a caller diffs against an acked-id
 *   set. The underlying BEHAVIOR (a retry replaces the failed entry
 *   under a fresh id; a race between a retry's ack and the old failure)
 *   is covered instead by tests/surface/nativeSend.test.tsx's
 *   `retrySend` cases (state-machine tier) and this file's own
 *   "late intent_rejected ack ... does not resurrect" case above.
 *
 * - "pending entries are scoped per session — switching sessions hides
 *   the other's pending" — NOT ported as a false-parity match: legacy's
 *   variant kept ALL sessions' pending entries in one shared, App-level
 *   list keyed by session id so ONE list held every session's pending
 *   entries side by side. Native's `ProvisionalSendRegistry` (state.ts) is
 *   ALSO session-id-keyed and now ALSO durable across a session switch
 *   (the historical gap this comment used to document was closed —
 *   `SurfaceStore.hydrate()`'s `reseedProvisionalSends` restores whatever
 *   the registry still holds for that session id, surviving the
 *   `useSurfaceStore.ts` dispose/recreate cycle) — but the shapes still
 *   differ enough that porting this exact case would test the wrong
 *   thing: legacy exposed the FULL cross-session list to one call site;
 *   native's registry is queried per-session-id only (there is no single
 *   caller that reads "every session's pending entries" at once). The
 *   underlying property (session A's provisional entry is untouched by
 *   switching to session B and back) is covered instead by
 *   tests/surface/nativeSend.test.tsx's "provisional-send survival across
 *   session switch" describe block, including its "does not leak a
 *   provisional send into a DIFFERENT session's store" case — the direct
 *   native analogue of this one.
 *
 * - "user_message_persisted for a different session does NOT touch the
 *   active pending" — redundant with existing native coverage: each
 *   session's SurfaceStore only ever subscribes to and reconciles frames
 *   for its OWN `surface_id`, so a wrong-session frame touching another
 *   session's state is structurally impossible, not just untested. Cross-
 *   session store isolation is already asserted by
 *   tests/session-switch-optimistic.test.tsx's "switching sessions swaps
 *   to an isolated store" case (migrate-2, Round 2).
 *
 * - "retrying a persisted failed prompt immediately appends a pending
 *   retry" / "keeps a failed retry visible when its source was a
 *   persisted prompt" / "replaces an optimistic failed prompt when its
 *   retry cannot send" — all three seed an ALREADY-PERSISTED failed user
 *   message (status: "error" baked into the session's server-side
 *   `messages`, not a same-session in-flight send) and click ITS OWN
 *   Retry button. Native has no mechanism to resend a persisted historical
 *   failed prompt at all today — `FailureChip` (src/surface/leaf/Chips.tsx)
 *   renders copy/expand chrome only, no retry-send action, and this
 *   round's own `PendingSendStatus`/`SurfaceStore.retrySend` only apply to
 *   a send that is STILL an unacked client-side provisional in the
 *   current browser session (SurfaceStore.pendingByIntentId has no entry
 *   for a prompt the backend already journaled and terminated in a PRIOR
 *   session). A real, currently-open native-path gap — not built here
 *   (would need a `failure`/turn-level "resend this turn's prompt" intent
 *   mapping, the same class of open product question as this round's
 *   Alter/edit-resend investigation) — flagged for orchestrator judgment,
 *   not silently faked.
 */
