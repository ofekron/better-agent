import { describe, expect, it } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { UserInteractionRequest } from "../src/types";

function makeApproval(sessionId: string): UserInteractionRequest {
  return {
    request_id: "approval-1",
    app_session_id: sessionId,
    kind: "approval",
    prompt: "Deploy the release?",
    status: "pending",
    created_at: Date.now() / 1000,
  };
}

describe("request user approval card", () => {
  it("offers exactly approve or alternative text", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session], userInputs: [makeApproval(session.id)] },
    });
    await h.selectSession(session.id);
    await h.flush();

    const card = h.$('[data-testid="user-approval-card"]');
    expect(card?.textContent).toContain("Deploy the release?");
    expect(card?.querySelectorAll("button")).toHaveLength(2);
    expect(card?.querySelector('button[data-action="approve"]')).not.toBeNull();
    expect(card?.querySelector('textarea[data-action="alternative"]')).not.toBeNull();
    h.unmount();
  });

  it("submits non-empty alternative text as a rejected approval", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session], userInputs: [makeApproval(session.id)] },
    });
    await h.selectSession(session.id);
    await h.flush();

    const textarea = h.$('textarea[data-action="alternative"]') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Run smoke tests first" } });
    await h.flush();
    await h.click('button[data-action="submit-alternative"]');

    const call = h.restCalls.find(
      (entry) => entry.method === "POST" && entry.path === "/api/user-input/approval-1/resolve",
    );
    expect(call?.body).toEqual({
      app_session_id: session.id,
      approved: false,
      alternative: "Run smoke tests first",
    });
    h.unmount();
  });
});
