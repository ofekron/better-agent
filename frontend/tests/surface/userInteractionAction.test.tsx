// UserInteractionActionView — a pending user_interaction node of a
// recognized kind renders the REAL interactive form (submit path mocked
// at the fetch level, hitting the SAME /api/user-input/{id}/resolve|cancel
// endpoints the legacy cards use); resolved/cancelled/unrecognized-kind
// nodes render the read-only state chip instead.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UserInteractionActionView } from "../../src/surface/nodes/UserInteractionAction";
import { node, resetSeq } from "./fixtures";
import type { UserInteractionPayloadWire } from "../../src/adapter/wire";

resetSeq();

function interactionNode(payload: UserInteractionPayloadWire) {
  return node({
    node_id: "ui1",
    turn_id: "t1",
    kind: "user_interaction",
    payload,
  });
}

describe("UserInteractionActionView", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("kind=approval, pending: renders the real approval form and POSTs resolve on Approve", async () => {
    const n = interactionNode({
      kind: "approval",
      state: "pending",
      request: { request_id: "req1", app_session_id: "s1", prompt: "Delete the file?" },
      response: null,
    });
    render(<UserInteractionActionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("user-approval-card")).toBeTruthy());
    expect(screen.getByText("Delete the file?")).toBeTruthy();

    await userEvent.click(screen.getByTestId("user-approval-card").querySelector('[data-action="approve"]')!);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/user-input/req1/resolve");
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      app_session_id: "s1",
      approved: true,
    });
  });

  it("kind=input, pending: renders the real question form", async () => {
    const n = interactionNode({
      kind: "input",
      state: "pending",
      request: {
        request_id: "req2",
        app_session_id: "s1",
        questions: [{ id: "q1", header: "Env", question: "Which environment?", options: [{ label: "prod" }, { label: "staging" }] }],
      },
      response: null,
    });
    render(<UserInteractionActionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("user-input-card")).toBeTruthy());
    expect(screen.getByText("Which environment?")).toBeTruthy();
  });

  it("kind=memory, pending: renders the real memory-proposal editor", async () => {
    const n = interactionNode({
      kind: "memory",
      state: "pending",
      request: {
        request_id: "req3",
        app_session_id: "s1",
        memory_proposal: {
          action: "add",
          name: "Prefers dark mode",
          description: "User prefers dark mode",
          type: "user",
          content: "Always use dark mode",
          scope_type: "global",
          scope_path: "",
        },
      },
      response: null,
    });
    render(<UserInteractionActionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("memory-proposal-card")).toBeTruthy());
  });

  it("resolved node: renders the read-only state chip, not a form", () => {
    const n = interactionNode({
      kind: "approval",
      state: "resolved",
      request: { request_id: "req1", app_session_id: "s1", prompt: "Delete the file?" },
      response: { approved: true },
    });
    render(<UserInteractionActionView node={n} />);

    expect(screen.getByTestId("surface-user-interaction-chip")).toBeTruthy();
    expect(screen.queryByTestId("user-approval-card")).toBeNull();
  });

  it("unrecognized kind: falls back to the state chip even while pending", () => {
    const n = interactionNode({
      kind: "some_future_kind",
      state: "pending",
      request: {},
      response: null,
    });
    render(<UserInteractionActionView node={n} />);

    expect(screen.getByTestId("surface-user-interaction-chip")).toBeTruthy();
  });
});
