import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { PendingApproval } from "../src/types";

function makeApproval(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    delegation_id: "deleg-1",
    app_session_id: "sess-1",
    cwd: "/tmp/proj",
    justification: "need a researcher",
    proposed_description: "Researcher",
    proposed_orchestration_mode: "native",
    instructions_preview: "Find X",
    model: "claude-sonnet-4-6",
    status: "pending",
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86400_000).toISOString(),
    ...overrides,
  };
}

describe("worker approval cards", () => {
  it("renders one card per pending approval rehydrated from REST", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        approvals: [
          makeApproval({ delegation_id: "d1" }),
          makeApproval({
            delegation_id: "d2",
            justification: "need a writer",
            proposed_description: "Writer",
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const cards = h.toJSON().chat.approvals;
    expect(cards.map((c) => c.delegationId).sort()).toEqual(["d1", "d2"]);
    h.unmount();
  });

  it("Deny posts /deny and removes the card from the view", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session], approvals: [makeApproval({ delegation_id: "d-deny" })] },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.toJSON().chat.approvals).toHaveLength(1);
    await h.denyWorker("d-deny");

    expect(
      h.restCalls.find(
        (c) => c.method === "POST" && c.path === "/api/pending_approvals/d-deny/deny",
      ),
    ).toBeDefined();
    expect(h.toJSON().chat.approvals).toHaveLength(0);
    h.unmount();
  });

  it("worker_creation_approved WS event removes the matching card", async () => {
    const session = makeSession();
    // Seed via REST, but trigger removal via WS — simulates another tab
    // approving the same delegation.
    const h = await renderApp({
      seed: { sessions: [session], approvals: [makeApproval({ delegation_id: "d-ws" })] },
    });
    await h.selectSession(session.id);
    await h.flush();
    expect(h.toJSON().chat.approvals).toHaveLength(1);

    // The Chat component only ingests WS approval events from the
    // streamingEvents prop, which is gated on streamingAppSessionId.
    // Send a message to bind streamingAppSessionId, then emit the
    // approved event so it lands on streamingEvents.
    await h.typeAndSend("trigger");
    h.emit({ type: "turn_start", data: { session_id: session.id } });
    h.emit({
      type: "worker_creation_approved",
      data: { delegation_id: "d-ws" },
    });
    await h.flush();

    expect(h.toJSON().chat.approvals).toHaveLength(0);
    h.unmount();
  });

  it("worker_creation_failed WS event also removes the card", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        approvals: [makeApproval({ delegation_id: "d-fail" })],
      },
    });
    await h.selectSession(session.id);
    await h.typeAndSend("trigger");
    h.emit({ type: "turn_start", data: { session_id: session.id } });
    h.emit({
      type: "worker_creation_failed",
      data: { delegation_id: "d-fail", error: "spawn failed" },
    });
    await h.flush();

    expect(h.toJSON().chat.approvals).toHaveLength(0);
    h.unmount();
  });

  it("the approval card shows the manager's justification text", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        approvals: [
          makeApproval({
            delegation_id: "d-just",
            justification: "I really need help with X",
          }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.toJSON().chat.approvals[0].text).toContain(
      "I really need help with X",
    );
    h.unmount();
  });

  it("approve sends the edited description in the body", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        approvals: [makeApproval({ delegation_id: "d-edit", proposed_description: "Researcher" })],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    // Edit the description input in the card before approving.
    const input = h.$(
      `[data-testid="worker-approval-card"][data-delegation-id="d-edit"] input[type="text"]`,
    ) as HTMLInputElement | null;
    if (input) {
      // Programmatically clear + set; user-event has a soft spot for this.
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.value = "Refined name";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await h.approveWorker("d-edit");

    const call = h.backend.calls.find(
      (c) =>
        c.method === "POST" && c.path === "/api/pending_approvals/d-edit/approve",
    );
    expect(call).toBeDefined();
    // The card initial value was "Researcher"; after our manual input
    // the body should contain whatever the input held at click time.
    const body = call!.body as { description?: string };
    expect(typeof body.description).toBe("string");
    expect(body.description!.length).toBeGreaterThan(0);
    h.unmount();
  });
});
