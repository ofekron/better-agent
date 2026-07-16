import { describe, it, expect, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

afterEach(cleanup);

describe("session_reconciled refetch source", () => {
  it("refetches via /api/chat-tree — the same grammar as cold load — never the legacy session snapshot", async () => {
    const session = makeSession({
      id: "root-1",
      messages: [
        makeUserMsg({ id: "u1", content: "hi", seq: 1 }),
        makeAssistantMsg({ id: "a1", content: "done", seq: 2 }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const chatTreeGets = () =>
      h.restCalls.filter(
        (c) => c.method === "GET" && c.path === `/api/chat-tree/${session.id}`,
      ).length;
    const legacySnapshotGets = () =>
      h.restCalls.filter(
        (c) => c.method === "GET" && c.path === `/api/sessions/${session.id}`,
      ).length;

    const before = chatTreeGets();
    expect(before).toBeGreaterThan(0);

    h.emit({ type: "session_reconciled", data: { root_id: session.id } });
    await h.flush();

    expect(chatTreeGets()).toBe(before + 1);
    expect(legacySnapshotGets()).toBe(0);
  });
});
