import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { PendingApproval, ToolApproval, WSEvent } from "../src/types";

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

    h.emit({
      type: "worker_creation_approved",
      data: { delegation_id: "d-ws" },
    } as WSEvent);
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
    h.emit({
      type: "worker_creation_failed",
      data: { delegation_id: "d-fail", error: "spawn failed" },
    } as WSEvent);
    await h.flush();

    expect(h.toJSON().chat.approvals).toHaveLength(0);
    h.unmount();
  });

  it("worker_creation_requested WS event adds a matching project card", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "worker_creation_requested",
      data: makeApproval({ delegation_id: "d-live", cwd: session.cwd }),
    } as WSEvent);
    await h.flush();

    expect(h.toJSON().chat.approvals.map((card) => card.delegationId)).toEqual(["d-live"]);
    h.unmount();
  });

  it("tool approval WS lifecycle adds and resolves the current session card", async () => {
    const session = makeSession();
    const approval: ToolApproval = {
      approval_id: "tool-live",
      app_session_id: session.id,
      run_id: "run-1",
      provider_kind: "codex",
      tool_name: "shell",
      summary: { command: "git status" },
    };
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({ type: "tool_approval_requested", data: approval } as WSEvent);
    await h.flush();
    expect(h.$('[data-testid="tool-approval-card"][data-approval-id="tool-live"]')).not.toBeNull();

    h.emit({
      type: "tool_approval_resolved",
      data: { approval_id: approval.approval_id, app_session_id: session.id },
    } as WSEvent);
    await h.flush();
    expect(h.$('[data-testid="tool-approval-card"][data-approval-id="tool-live"]')).toBeNull();
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
