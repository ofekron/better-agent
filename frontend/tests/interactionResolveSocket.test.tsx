// Coverage for `src/lib/interactionResolveSocket.ts` (ADR 0006 §5's
// UserInteraction `resolve` intent write path) + the 3 card cores in
// `src/components/UserInteractionCards.tsx` resolving through it.
//
// `interactionResolveSocket` is globally stubbed in tests/setup.ts (same
// class of always-on module-level singleton as `sessionSurfaceRegistry`,
// see that stub's comment) — this file un-mocks it to exercise the REAL
// module, same escape hatch every other dedicated-coverage file in this
// suite uses (`vi.unmock`, see e.g. tests/useProviderSurfaceSocket.test.ts's
// sibling pattern for the provider plane's own shared connection). Every
// test re-imports the module fresh via `vi.resetModules()` — same
// isolation pattern `useProviderSurfaceSocket.test.ts` uses for its own
// true module-level singleton.

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocketController } from "./harness/mockWebSocket";

vi.unmock("../src/lib/interactionResolveSocket");

let ws: MockWebSocketController;

beforeEach(() => {
  ws = new MockWebSocketController();
  ws.install();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }));
});

afterEach(() => {
  ws.uninstall();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("submitResolveInteraction", () => {
  it("returns null when the shared connection is not open", async () => {
    vi.resetModules();
    const { submitResolveInteraction } = await import("../src/lib/interactionResolveSocket");
    expect(
      submitResolveInteraction("sess-1", "tool_approval:a1", "approval", { decision: "approve" }),
    ).toBeNull();
  });

  it("opens the shared connection on the first acquirer and closes it once released", async () => {
    vi.resetModules();
    const { acquireInteractionResolveSocket, isInteractionResolveSocketOpen } = await import(
      "../src/lib/interactionResolveSocket"
    );
    const release = acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(isInteractionResolveSocketOpen()).toBe(true));
    release();
    expect(isInteractionResolveSocketOpen()).toBe(false);
  });

  it("sends the correctly-shaped resolve intent once open (approval kind)", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const release = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    mod.submitResolveInteraction("sess-1", "worker_approval:d1", "approval", { decision: "deny" });

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent).toMatchObject({
      kind: "resolve",
      session_id: "sess-1",
      interaction_ref: "worker_approval:d1",
      interaction_kind: "approval",
      response: { decision: "deny" },
    });
    expect(typeof sent!.intent.intent_id).toBe("string");
    expect(sent!.intent.intent_id).not.toBe("");
    release();
  });

  it("sends the double-nested response shape for input kind (matches backend's InputResponse.response)", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const release = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    mod.submitResolveInteraction("sess-1", "user_input:r1", "input", { response: { answers: { q1: "yes" } } });

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent!.intent.response).toEqual({ response: { answers: { q1: "yes" } } });
    release();
  });

  it("resolves the ack promise on intent_accepted", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const release = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    const ackPromise = mod.submitResolveInteraction("sess-1", "tool_approval:a1", "approval", { decision: "approve" });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: sent.intent.intent_id } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_accepted");
    release();
  });

  it("shares one connection across multiple acquirers, closing only once every one releases", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const releaseA = mod.acquireInteractionResolveSocket();
    const releaseB = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    releaseA();
    expect(mod.isInteractionResolveSocketOpen()).toBe(true);
    releaseB();
    expect(mod.isInteractionResolveSocketOpen()).toBe(false);
  });
});

describe("tryResolveInteraction", () => {
  it("returns false (not true/ack-object) when the shared connection is not open", async () => {
    vi.resetModules();
    ws.uninstall();
    ws = new MockWebSocketController(false);
    ws.install();
    const { tryResolveInteraction } = await import("../src/lib/interactionResolveSocket");
    await expect(
      tryResolveInteraction("sess-1", "delegate_choice:d1", "choice", { picked_ref: "sess-2" }),
    ).resolves.toBe(false);
  });

  it("sends the exact delegate-choice wire shape and returns true on intent_accepted (App.tsx's resolveDelegation contract)", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const release = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    const resultPromise = mod.tryResolveInteraction(
      "sess-1",
      `${mod.DELEGATE_CHOICE_REF_PREFIX}d1`,
      "choice",
      { picked_ref: "sess-2" },
    );
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent).toMatchObject({
      kind: "resolve",
      session_id: "sess-1",
      interaction_ref: "delegate_choice:d1",
      interaction_kind: "choice",
      response: { picked_ref: "sess-2" },
    });

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: sent!.intent.intent_id } as never);
    });
    await expect(resultPromise).resolves.toBe(true);
    release();
  });

  it("returns true (handled, no REST retry) even on a genuine intent_rejected ack", async () => {
    vi.resetModules();
    const mod = await import("../src/lib/interactionResolveSocket");
    const release = mod.acquireInteractionResolveSocket();
    await vi.waitFor(() => expect(mod.isInteractionResolveSocketOpen()).toBe(true));

    const resultPromise = mod.tryResolveInteraction(
      "sess-1", "delegate_choice:d1", "choice", { picked_ref: null },
    );
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: { intent_id: string } };
    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "not_found" } as never);
    });
    await expect(resultPromise).resolves.toBe(true);
    release();
  });
});

describe("UserInteractionCards — native resolve intent with legacy REST fallback", () => {
  it("UserApprovalCardCore resolves via the v2 intent when the shared connection is open (no REST call)", async () => {
    vi.resetModules();
    const { UserApprovalCardCore } = await import("../src/components/UserInteractionCards");
    render(<UserApprovalCardCore requestId="req1" appSessionId="sess-1" prompt="Deploy?" onDone={() => {}} />);

    // Connection is warmed by the card's own useInteractionResolveSocket() —
    // wait for it to actually reach OPEN before clicking, matching the
    // real "connection had time to establish" case this test targets.
    const { isInteractionResolveSocketOpen } = await import("../src/lib/interactionResolveSocket");
    await vi.waitFor(() => expect(isInteractionResolveSocketOpen()).toBe(true));

    await userEvent.click(screen.getByTestId("user-approval-card").querySelector('[data-action="approve"]')!);

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: { intent_id: string; interaction_ref: string; interaction_kind: string; response: unknown } } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent.interaction_ref).toBe("user_input:req1");
    expect(sent!.intent.interaction_kind).toBe("input");
    expect(sent!.intent.response).toEqual({ response: { approved: true } });

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: sent!.intent.intent_id } as never);
    });

    await waitFor(() => expect(globalThis.fetch).not.toHaveBeenCalled());
  });

  it("UserApprovalCardCore falls back to legacy REST when the connection never opens", async () => {
    vi.resetModules();
    ws.uninstall();
    // A connection that never reaches OPEN (stalled handshake / offline) —
    // `autoOpen: false` mirrors `MockWebSocketController`'s own documented
    // use for exercising the reconnect/not-open path (see its `closeCurrent`/
    // `reopenCurrent`), without the unrealistic synchronous-throw shape a
    // real WebSocket constructor never actually has.
    ws = new MockWebSocketController(false);
    ws.install();

    const { UserApprovalCardCore } = await import("../src/components/UserInteractionCards");
    render(<UserApprovalCardCore requestId="req1" appSessionId="sess-1" prompt="Deploy?" onDone={() => {}} />);

    await userEvent.click(screen.getByTestId("user-approval-card").querySelector('[data-action="approve"]')!);

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/api/user-input/req1/resolve");
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      app_session_id: "sess-1",
      approved: true,
    });
  });

  it("UserInputCardCore resolves via the v2 intent with the answers wrapped as the free-form response body", async () => {
    vi.resetModules();
    const { UserInputCardCore } = await import("../src/components/UserInteractionCards");
    render(
      <UserInputCardCore
        requestId="req2"
        appSessionId="sess-1"
        questions={[{ id: "q1", header: "H", question: "Which?", options: [{ label: "prod" }] }]}
        onDone={() => {}}
      />,
    );
    const { isInteractionResolveSocketOpen } = await import("../src/lib/interactionResolveSocket");
    await vi.waitFor(() => expect(isInteractionResolveSocketOpen()).toBe(true));

    await userEvent.click(screen.getByText("prod"));
    await userEvent.click(screen.getByText("userInput.send"));

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "resolve",
    ) as { intent: { interaction_ref: string; response: unknown } } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent.interaction_ref).toBe("user_input:req2");
    expect(sent!.intent.response).toEqual({ response: { answers: { q1: "prod" } } });
  });
});
